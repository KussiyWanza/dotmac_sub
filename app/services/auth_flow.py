from __future__ import annotations

import hashlib
import logging
import os
import secrets
import string
import warnings
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID

import pyotp
from cryptography.fernet import Fernet, InvalidToken

# Named `held_secret` at the import, not `get_secret`: `app.services.secrets`
# exports a `get_secret(path, field)` that TALKS TO OpenBao, and at a call site
# the two would be indistinguishable. This one is a dict lookup over material
# loaded once at boot — see `app/services/kernel_secret_source.py`.
from dotmac_kernel.secret_sources import get_secret as held_secret
from fastapi import HTTPException, Request, Response, status
from jose import JWTError, jwt
from passlib.context import CryptContext
from sqlalchemy import func
from sqlalchemy import select as sa_select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.models.auth import (
    AuthProvider,
    MFAMethod,
    MFAMethodType,
    MFARecoveryCode,
    SessionStatus,
    UserCredential,
)
from app.models.auth import (
    Session as AuthSession,
)
from app.models.domain_settings import DomainSetting, SettingDomain
from app.models.rbac import (
    Permission,
    Role,
    RolePermission,
    SubscriberRole,
    SystemUserPermission,
    SystemUserRole,
)
from app.models.subscriber import ResellerUser, Subscriber
from app.models.system_user import SystemUser
from app.request_meta import client_ip
from app.schemas.auth_flow import LoginResponse, LogoutResponse, TokenResponse
from app.services import auth_cache, staff_party_authentication
from app.services import radius_auth as radius_auth_service
from app.services.capability_recipient import resolve_capability_recipient
from app.services.common import coerce_uuid
from app.services.credential_crypto import decrypt_credential, encrypt_credential
from app.services.response import ListResponseMixin
from app.services.secrets import resolve_secret
from app.services.settings_spec import resolve_value

logger = logging.getLogger(__name__)

PASSWORD_CONTEXT = CryptContext(
    schemes=["pbkdf2_sha256", "bcrypt", "sha512_crypt"],
    default="pbkdf2_sha256",
    deprecated="auto",
)


def _env_value(name: str) -> str | None:
    value = os.getenv(name)
    if value is None or value == "":
        return None
    return value


def _env_int(name: str) -> int | None:
    raw = _env_value(name)
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _now() -> datetime:
    return datetime.now(UTC)


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def lockout_detail(
    prefix: str,
    *,
    locked_until: datetime | None = None,
    retry_after_seconds: int | None = None,
) -> str:
    remaining_seconds = 0
    normalized_until = _as_utc(locked_until)
    if normalized_until:
        remaining_seconds = max(int((normalized_until - _now()).total_seconds()), 0)
    elif retry_after_seconds is not None:
        remaining_seconds = max(int(retry_after_seconds), 0)

    if remaining_seconds <= 0:
        return f"{prefix}. Please try again later."
    minutes = max(1, (remaining_seconds + 59) // 60)
    unit = "minute" if minutes == 1 else "minutes"
    return f"{prefix}. Try again in {minutes} {unit}."


def duration_label(seconds: int) -> str:
    seconds = max(int(seconds), 1)
    for unit, unit_seconds in (
        ("day", 86400),
        ("hour", 3600),
        ("minute", 60),
    ):
        if seconds >= unit_seconds:
            value = max(1, (seconds + unit_seconds - 1) // unit_seconds)
            suffix = unit if value == 1 else f"{unit}s"
            return f"{value} {suffix}"
    return "1 minute"


def _truncate_user_agent(value: str | None, max_len: int = 512) -> str | None:
    if not value:
        return value
    if len(value) <= max_len:
        return value
    return value[:max_len]


def _clean_device_id(value: str | None, max_len: int = 64) -> str | None:
    """Normalise the client-supplied X-Device-Id (a per-install opaque id).
    Returns None when absent/blank so non-native callers keep the old
    one-session-per-login behaviour."""
    if not value:
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    return cleaned[:max_len]


def _setting_value(db: Session | None, key: str) -> str | None:
    if db is None:
        return None
    setting = (
        db.query(DomainSetting)
        .filter(DomainSetting.domain == SettingDomain.auth)
        .filter(DomainSetting.key == key)
        .filter(DomainSetting.is_active.is_(True))
        .first()
    )
    if not setting:
        return None
    if setting.value_text:
        return cast(str, setting.value_text)
    if setting.value_json is not None:
        return str(setting.value_json)
    return None


def _jwt_secret(db: Session | None) -> str:
    """The signing secret: the environment, else what was held at boot.

    The `auth/jwt_secret` SETTING is no longer consulted. It never held the
    secret — it held a `bao://` reference that this call dereferenced, so every
    token signed and every token verified could reach OpenBao over the network
    while handling a request. Starter ADR-0009 forbids exactly that: a secret
    is held, never dereferenced. The row's dereference also had no timeout of
    its own and no error path — a slow store made signing slow.

    Environment first, preserving the precedence this function already had.
    """

    secret = resolve_secret(_env_value("JWT_SECRET")) or held_secret("jwt_secret")
    if not secret:
        raise HTTPException(status_code=500, detail="JWT secret not configured")
    return secret


def _jwt_algorithm(db: Session | None) -> str:
    return _env_value("JWT_ALGORITHM") or _setting_value(db, "jwt_algorithm") or "HS256"


def _access_ttl_minutes(db: Session | None) -> int:
    env_value = _env_int("JWT_ACCESS_TTL_MINUTES")
    if env_value is not None:
        return env_value
    value = _setting_value(db, "jwt_access_ttl_minutes")
    if value is not None:
        try:
            return int(value)
        except ValueError:
            return 15
    return 15


def _refresh_ttl_days(db: Session | None) -> int:
    env_value = _env_int("JWT_REFRESH_TTL_DAYS")
    if env_value is not None:
        return env_value
    value = _setting_value(db, "jwt_refresh_ttl_days")
    if value is not None:
        try:
            return int(value)
        except ValueError:
            return 30
    return 30


def _totp_issuer(db: Session | None) -> str:
    return _env_value("TOTP_ISSUER") or _setting_value(db, "totp_issuer") or "dotmac_sm"


def _force_admin_mfa(db: Session | None) -> bool:
    value = _env_value("ADMIN_MFA_REQUIRED")
    if value is None:
        value = _setting_value(db, "admin_mfa_required")
    if value is None:
        value = _setting_value(db, "force_2fa")
    if value is None:
        value = resolve_value(db, SettingDomain.auth, "admin_mfa_required")
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _setting_int(
    db: Session | None, key: str, default: int, *, minimum: int = 1
) -> int:
    value = resolve_value(db, SettingDomain.auth, key)
    try:
        parsed = int(str(value))
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= minimum else default


def _refresh_cookie_name(db: Session | None) -> str:
    return (
        _env_value("REFRESH_COOKIE_NAME")
        or _setting_value(db, "refresh_cookie_name")
        or "refresh_token"
    )


def _wants_refresh_in_body(request: Request | None) -> bool:
    """Native clients (mobile) can't read the httpOnly refresh cookie, so they
    opt into receiving the refresh token in the JSON body via this header and
    persist it in the platform secure store instead. Browser clients omit the
    header and keep the safer httpOnly-cookie behaviour."""
    if request is None:
        return False
    return request.headers.get("x-auth-refresh-in-body", "").strip().lower() in {
        "1",
        "true",
        "yes",
    }


def _refresh_cookie_secure(db: Session | None) -> bool:
    env_value = _env_value("REFRESH_COOKIE_SECURE")
    if env_value is not None:
        return env_value.lower() in {"1", "true", "yes", "on"}
    value = _setting_value(db, "refresh_cookie_secure")
    if value is not None:
        return str(value).lower() in {"1", "true", "yes", "on"}
    # Secure-by-default: callers AND this with an HTTPS-request check, so plain
    # HTTP (local dev) still works while production never drops the flag over
    # TLS. Set REFRESH_COOKIE_SECURE=false to force-disable.
    return True


def _refresh_cookie_samesite(db: Session | None) -> str:
    return (
        _env_value("REFRESH_COOKIE_SAMESITE")
        or _setting_value(db, "refresh_cookie_samesite")
        or "lax"
    )


def _refresh_cookie_domain(db: Session | None) -> str | None:
    return _env_value("REFRESH_COOKIE_DOMAIN") or _setting_value(
        db, "refresh_cookie_domain"
    )


def _refresh_cookie_path(db: Session | None) -> str:
    return (
        _env_value("REFRESH_COOKIE_PATH")
        or _setting_value(db, "refresh_cookie_path")
        or "/auth"
    )


def _mfa_key(db: Session | None) -> bytes:
    """The TOTP-secret encryption key — held, not read from the database.

    Same reasoning as `_jwt_secret`: this key protects rows in the very
    database the reference used to live in, so it must not be stored there,
    and verifying one TOTP code must not depend on a network hop.
    """

    key = resolve_secret(_env_value("TOTP_ENCRYPTION_KEY")) or held_secret(
        "totp_encryption_key"
    )
    if not key:
        raise HTTPException(
            status_code=500, detail="TOTP encryption key not configured"
        )
    return key.encode()


def _fernet(db: Session | None) -> Fernet:
    try:
        return Fernet(_mfa_key(db))
    except ValueError as exc:
        raise HTTPException(
            status_code=500, detail="Invalid TOTP encryption key"
        ) from exc


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def principal_from_session(session: AuthSession) -> tuple[str, str]:
    """(principal_type, principal_id) for an AuthSession — subscriber, system_user,
    or reseller_user (Layer 3). Used by token issuance/refresh."""
    if session.system_user_id:
        return "system_user", str(session.system_user_id)
    if getattr(session, "reseller_user_id", None):
        return "reseller_user", str(session.reseller_user_id)
    return "subscriber", str(session.subscriber_id)


def auth_session_principal_filter(principal_type: str, principal_id):
    """AuthSession column filter for a principal — keeps session lookup/revocation
    correct for reseller_user principals (not just subscriber/system_user)."""
    if principal_type == "system_user":
        return AuthSession.system_user_id == principal_id
    if principal_type == "reseller_user":
        return AuthSession.reseller_user_id == principal_id
    return AuthSession.subscriber_id == principal_id


def credential_principal_filter(principal_type: str, principal_id):
    """UserCredential column filter for a principal (reseller_user-aware)."""
    if principal_type == "system_user":
        return UserCredential.system_user_id == principal_id
    if principal_type == "reseller_user":
        return UserCredential.reseller_user_id == principal_id
    return UserCredential.subscriber_id == principal_id


def _jwt_encode_token(payload: dict[str, Any], secret: str, algorithm: str) -> str:
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore",
            message=r"datetime\.datetime\.utcnow\(\) is deprecated.*",
            category=DeprecationWarning,
            module=r"jose\.jwt",
        )
        return cast(str, jwt.encode(payload, secret, algorithm=algorithm))


def _jwt_decode_token(token: str, secret: str, algorithm: str) -> dict[Any, Any]:
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore",
            message=r"datetime\.datetime\.utcnow\(\) is deprecated.*",
            category=DeprecationWarning,
            module=r"jose\.jwt",
        )
        return cast(dict[Any, Any], jwt.decode(token, secret, algorithms=[algorithm]))


def hash_session_token(token: str) -> str:
    return _hash_token(token)


def _issue_access_token(
    db: Session | None,
    principal_id: str,
    principal_type_or_session_id: str,
    session_id: str | None = None,
    roles: list[str] | None = None,
    permissions: list[str] | None = None,
) -> str:
    # Backward compatibility: older callers passed (db, principal_id, session_id, ...)
    # and implicitly targeted subscriber principals.
    if session_id is None:
        principal_type = "subscriber"
        resolved_session_id = principal_type_or_session_id
    else:
        principal_type = principal_type_or_session_id
        resolved_session_id = session_id

    now = _now()
    payload = {
        "sub": principal_id,
        "principal_id": principal_id,
        "principal_type": principal_type,
        "session_id": resolved_session_id,
        "typ": "access",
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=_access_ttl_minutes(db))).timestamp()),
    }
    if roles:
        payload["roles"] = roles
    if permissions:
        payload["scopes"] = permissions
    return _jwt_encode_token(payload, _jwt_secret(db), _jwt_algorithm(db))


def issue_impersonation_access_token(
    db: Session | None,
    subscriber_id: str,
    session_id: str,
    acting_subscriber_id: str,
    ttl_minutes: int = 15,
) -> str:
    """Short-lived customer-scoped token for reseller "view as customer".

    Carries ``imp``/``imp_by`` claims: the auth dependency enforces read-only
    (GET/HEAD/OPTIONS) for these tokens, and ``imp_by`` keeps the acting
    reseller attributable in request logs."""
    now = _now()
    payload = {
        "sub": subscriber_id,
        "principal_id": subscriber_id,
        "principal_type": "subscriber",
        "session_id": session_id,
        "typ": "access",
        "imp": True,
        "imp_by": acting_subscriber_id,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=ttl_minutes)).timestamp()),
    }
    return _jwt_encode_token(payload, _jwt_secret(db), _jwt_algorithm(db))


def issue_web_session_token(db: Session | None, access_token: str) -> str:
    """Issue a compact web session JWT for cookie transport.

    Web routes only need principal/session identity. Roles and scopes can be
    resolved server-side when required, which keeps the session cookie small.
    """
    payload = decode_access_token(db, access_token)
    principal_id = str(payload.get("principal_id") or payload.get("sub") or "")
    principal_type = str(payload.get("principal_type") or "subscriber")
    session_id = str(payload.get("session_id") or "")
    if not principal_id or not session_id:
        raise HTTPException(status_code=401, detail="Invalid access token")
    return _issue_access_token(db, principal_id, principal_type, session_id)


def _issue_mfa_token(
    db: Session | None,
    principal_id: str,
    principal_type: str = "subscriber",
    *,
    staff_binding: staff_party_authentication.StaffSessionBinding | None = None,
) -> str:
    now = _now()
    payload = {
        "sub": principal_id,
        "principal_id": principal_id,
        "principal_type": principal_type,
        "typ": "mfa",
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=5)).timestamp()),
    }
    if principal_type == "system_user":
        if staff_binding is None:
            raise staff_party_authentication.StaffProjectionError(
                staff_party_authentication.StaffProjectionRefusal.projection_missing,
                principal_id,
            )
        if str(staff_binding.system_user_id) != str(principal_id):
            raise staff_party_authentication.StaffProjectionError(
                staff_party_authentication.StaffProjectionRefusal.projection_conflict,
                principal_id,
            )
        payload["party_id"] = str(staff_binding.party_id)
    return _jwt_encode_token(payload, _jwt_secret(db), _jwt_algorithm(db))


def _issue_mfa_enrollment_token(
    db: Session | None,
    principal_id: str,
    principal_type: str = "system_user",
    *,
    staff_binding: staff_party_authentication.StaffSessionBinding | None = None,
) -> str:
    now = _now()
    payload = {
        "sub": principal_id,
        "principal_id": principal_id,
        "principal_type": principal_type,
        "typ": "mfa_enrollment",
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=5)).timestamp()),
    }
    if principal_type == "system_user":
        if staff_binding is None:
            raise staff_party_authentication.StaffProjectionError(
                staff_party_authentication.StaffProjectionRefusal.projection_missing,
                principal_id,
            )
        if str(staff_binding.system_user_id) != str(principal_id):
            raise staff_party_authentication.StaffProjectionError(
                staff_party_authentication.StaffProjectionRefusal.projection_conflict,
                principal_id,
            )
        payload["party_id"] = str(staff_binding.party_id)
    return _jwt_encode_token(payload, _jwt_secret(db), _jwt_algorithm(db))


def staff_binding_from_token_payload(
    payload: Mapping[str, object],
    *,
    invalid_detail: str,
) -> staff_party_authentication.StaffSessionBinding:
    """Parse the signed staff identity/context pair carried across an MFA hop."""

    principal_id = payload.get("principal_id") or payload.get("sub")
    party_id = payload.get("party_id")
    if not principal_id or not party_id:
        raise HTTPException(status_code=401, detail=invalid_detail)
    try:
        return staff_party_authentication.StaffSessionBinding(
            party_id=UUID(str(party_id)),
            system_user_id=UUID(str(principal_id)),
        )
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=401, detail=invalid_detail) from exc


# Admin reset links are capped to one hour regardless of the (customer-facing)
# password_reset_expiry_minutes setting; an explicit ttl_minutes still wins.
SYSTEM_USER_RESET_TTL_CAP_MINUTES = 60


def _password_reset_ttl_minutes(db: Session | None) -> int:
    env_value = _env_int("PASSWORD_RESET_EXPIRY_MINUTES")
    if env_value is None:
        env_value = _env_int("PASSWORD_RESET_TTL_MINUTES")
    if env_value is not None:
        return env_value
    value = _setting_value(db, "password_reset_expiry_minutes")
    if value is None:
        value = _setting_value(db, "password_reset_ttl_minutes")
    if value is not None:
        try:
            return int(value)
        except ValueError:
            return 1440
    return 1440


def _issue_password_reset_token(
    db: Session | None,
    principal_id: str,
    principal_type_or_email: str,
    email: str | None = None,
    *,
    ttl_minutes: int | None = None,
) -> str:
    # Backward compatibility: older callers passed (db, principal_id, email)
    # and implicitly targeted subscriber principals.
    if email is None:
        principal_type = "subscriber"
        resolved_email = principal_type_or_email
    else:
        principal_type = principal_type_or_email
        resolved_email = email

    now = _now()
    token_ttl_minutes = ttl_minutes if ttl_minutes and ttl_minutes > 0 else None
    if token_ttl_minutes is None:
        token_ttl_minutes = _password_reset_ttl_minutes(db)
    payload = {
        "sub": principal_id,
        "principal_id": principal_id,
        "principal_type": principal_type,
        "email": resolved_email,
        "typ": "password_reset",
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=token_ttl_minutes)).timestamp()),
    }
    return _jwt_encode_token(payload, _jwt_secret(db), _jwt_algorithm(db))


def _decode_password_reset_token(db: Session | None, token: str) -> dict:
    return _decode_jwt(db, token, "password_reset")


def _email_verification_ttl_minutes(db: Session | None) -> int:
    env_value = _env_int("EMAIL_VERIFICATION_EXPIRY_MINUTES")
    if env_value is None:
        env_value = _env_int("EMAIL_VERIFICATION_TTL_MINUTES")
    if env_value is not None:
        return env_value
    value = _setting_value(db, "email_verification_expiry_minutes")
    if value is None:
        value = _setting_value(db, "email_verification_ttl_minutes")
    if value is not None:
        try:
            return int(value)
        except ValueError:
            return 1440
    return 1440


def _issue_email_verification_token(
    db: Session | None,
    subscriber_id: str,
    email: str,
    *,
    ttl_minutes: int | None = None,
) -> str:
    now = _now()
    token_ttl_minutes = ttl_minutes if ttl_minutes and ttl_minutes > 0 else None
    if token_ttl_minutes is None:
        token_ttl_minutes = _email_verification_ttl_minutes(db)
    payload = {
        "sub": subscriber_id,
        "principal_id": subscriber_id,
        "principal_type": "subscriber",
        "email": email,
        "typ": "email_verification",
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=token_ttl_minutes)).timestamp()),
    }
    return _jwt_encode_token(payload, _jwt_secret(db), _jwt_algorithm(db))


def _decode_email_verification_token(db: Session | None, token: str) -> dict:
    return _decode_jwt(db, token, "email_verification")


def _decode_jwt(db: Session | None, token: str, expected_type: str) -> dict:
    try:
        payload = _jwt_decode_token(token, _jwt_secret(db), _jwt_algorithm(db))
    except JWTError as exc:
        raise HTTPException(status_code=401, detail="Invalid token") from exc
    if payload.get("typ") != expected_type:
        raise HTTPException(status_code=401, detail="Invalid token type")
    return payload


def decode_access_token(db: Session | None, token: str) -> dict:
    return _decode_jwt(db, token, "access")


def _subscriber_or_404(db: Session, subscriber_id: str) -> Subscriber:
    subscriber = cast(Subscriber | None, db.get(Subscriber, coerce_uuid(subscriber_id)))
    if not subscriber:
        raise HTTPException(status_code=404, detail="Subscriber not found")
    return subscriber


def _person_or_404(db: Session, person_id: str) -> Subscriber:
    """Backwards-compatible helper: people are subscribers in this codebase."""
    return _subscriber_or_404(db, person_id)


def _load_rbac_claims(
    db: Session,
    principal_type_or_principal_id: str,
    principal_id: str | None = None,
):
    if db is None:
        return [], []
    if principal_id is None:
        principal_type = "subscriber"
        resolved_principal_id = principal_type_or_principal_id
    else:
        principal_type = principal_type_or_principal_id
        resolved_principal_id = principal_id
    cached = auth_cache.get_claims(principal_type, str(resolved_principal_id))
    if cached is not None:
        return cached
    if principal_type == "reseller_user":
        # Reseller portal authorization is enforced via reseller_users
        # membership (reseller_portal._get_reseller_user), not RBAC roles. A
        # reseller_user principal carries no system/subscriber roles.
        auth_cache.set_claims(principal_type, str(resolved_principal_id), [], [])
        return [], []
    principal_uuid = coerce_uuid(resolved_principal_id)
    if principal_type == "system_user":
        roles = (
            db.query(Role)
            .join(SystemUserRole, SystemUserRole.role_id == Role.id)
            .filter(SystemUserRole.system_user_id == principal_uuid)
            .filter(Role.is_active.is_(True))
            .all()
        )
        permissions = (
            db.query(Permission)
            .join(RolePermission, RolePermission.permission_id == Permission.id)
            .join(Role, RolePermission.role_id == Role.id)
            .join(SystemUserRole, SystemUserRole.role_id == Role.id)
            .filter(SystemUserRole.system_user_id == principal_uuid)
            .filter(Role.is_active.is_(True))
            .filter(Permission.is_active.is_(True))
            .all()
        )
        direct_permissions = (
            db.query(Permission)
            .join(
                SystemUserPermission,
                SystemUserPermission.permission_id == Permission.id,
            )
            .filter(SystemUserPermission.system_user_id == principal_uuid)
            .filter(Permission.is_active.is_(True))
            .all()
        )
    else:
        roles = (
            db.query(Role)
            .join(SubscriberRole, SubscriberRole.role_id == Role.id)
            .filter(SubscriberRole.subscriber_id == principal_uuid)
            .filter(Role.is_active.is_(True))
            .all()
        )
        permissions = (
            db.query(Permission)
            .join(RolePermission, RolePermission.permission_id == Permission.id)
            .join(Role, RolePermission.role_id == Role.id)
            .join(SubscriberRole, SubscriberRole.role_id == Role.id)
            .filter(SubscriberRole.subscriber_id == principal_uuid)
            .filter(Role.is_active.is_(True))
            .filter(Permission.is_active.is_(True))
            .all()
        )
        direct_permissions = []
    role_names = [role.name for role in roles]
    permission_keys = list({perm.key for perm in [*permissions, *direct_permissions]})
    auth_cache.set_claims(
        principal_type,
        str(resolved_principal_id),
        role_names,
        permission_keys,
    )
    return role_names, permission_keys


def _resolve_login_credential(
    db: Session,
    *,
    provider: AuthProvider,
    identifier: str,
) -> UserCredential | None:
    """Resolve active credential by login identity.

    Login identity is the credential ``username`` (customers and resellers) or,
    for admins, the ``system_users.email``. Subscriber email is deliberately NOT
    a login key: it is non-unique contact information (many customers share an
    address), so matching on it would be ambiguous.
    """
    normalized_identifier = identifier.strip()
    if not normalized_identifier:
        return None

    return cast(
        UserCredential | None,
        db.query(UserCredential)
        .outerjoin(SystemUser, SystemUser.id == UserCredential.system_user_id)
        .filter(UserCredential.provider == provider)
        .filter(UserCredential.is_active.is_(True))
        .filter(
            (UserCredential.username == normalized_identifier)
            | (func.lower(SystemUser.email) == normalized_identifier.lower())
        )
        .order_by(UserCredential.created_at.desc())
        .first(),
    )


def _principal_for_credential(
    db: Session, credential: UserCredential
) -> tuple[str, str, object | None]:
    if credential.system_user_id:
        # `system_user_id` still discriminates the KIND of principal — staff
        # rather than subscriber or reseller — because SystemUser remains the
        # product-owned staff context. It no longer supplies the IDENTITY: that
        # comes from the Party projection, with no legacy fallback.
        # `staff_party_authentication` raises rather than returning None, so an
        # unresolvable projection cannot be mistaken for an anonymous principal.
        principal = staff_party_authentication.resolve_staff_principal(db, credential)
        return "system_user", str(principal.id), principal
    if (
        getattr(credential, "reseller_user_id", None)
        and settings.reseller_user_principal_enabled
    ):
        return (
            "reseller_user",
            str(credential.reseller_user_id),
            db.get(ResellerUser, credential.reseller_user_id),
        )
    if credential.subscriber_id:
        return (
            "subscriber",
            str(credential.subscriber_id),
            db.get(Subscriber, credential.subscriber_id),
        )
    return "subscriber", "", None


def _primary_totp_method(
    db: Session, principal_type: str, principal_id: str
) -> MFAMethod | None:
    query = db.query(MFAMethod).filter(MFAMethod.method_type == MFAMethodType.totp)
    if principal_type == "system_user":
        query = query.filter(MFAMethod.system_user_id == coerce_uuid(principal_id))
    elif principal_type == "reseller_user":
        query = query.filter(MFAMethod.reseller_user_id == coerce_uuid(principal_id))
    else:
        query = query.filter(MFAMethod.subscriber_id == coerce_uuid(principal_id))
    return cast(
        MFAMethod | None,
        query.filter(MFAMethod.is_active.is_(True))
        .filter(MFAMethod.enabled.is_(True))
        .filter(MFAMethod.is_primary.is_(True))
        .first(),
    )


def _encrypt_secret(db: Session | None, secret: str) -> str:
    return _fernet(db).encrypt(secret.encode("utf-8")).decode("utf-8")


def _decrypt_secret(db: Session | None, secret: str) -> str:
    try:
        return _fernet(db).decrypt(secret.encode("utf-8")).decode("utf-8")
    except InvalidToken as exc:
        raise HTTPException(status_code=500, detail="Invalid MFA secret") from exc


def hash_password(password: str) -> str:
    return cast(str, PASSWORD_CONTEXT.hash(password))


def hash_service_secret(password: str) -> str:
    """Store subscriber service credentials in reversible-at-rest format.

    PPPoE and other RADIUS flows may require Cleartext-Password or NT-Password
    for MS-CHAP-compatible auth. We therefore store service credentials using
    the shared credential encryption layer instead of a one-way hash.
    """
    return cast(str, encrypt_credential(password))


def verify_password(password: str, password_hash: str | None) -> bool:
    if not password_hash:
        return False
    decrypted = decrypt_credential(password_hash)
    if decrypted != password_hash:
        return secrets.compare_digest(password, decrypted or "")
    return cast(bool, PASSWORD_CONTEXT.verify(password, password_hash))


LOGIN_MAX_FAILED_ATTEMPTS = 5
LOGIN_LOCKOUT_MINUTES = 15


def _admin_login_max_failed_attempts(db: Session | None) -> int:
    return _setting_int(
        db,
        "admin_login_max_attempts",
        LOGIN_MAX_FAILED_ATTEMPTS,
    )


def _admin_login_lockout_minutes(db: Session | None) -> int:
    return _setting_int(
        db,
        "admin_lockout_minutes",
        LOGIN_LOCKOUT_MINUTES,
    )


def _record_login_failure(db: Session, credential: UserCredential, now) -> None:
    credential.failed_login_attempts += 1
    if credential.failed_login_attempts >= _admin_login_max_failed_attempts(db):
        credential.locked_until = now + timedelta(
            minutes=_admin_login_lockout_minutes(db)
        )
    db.commit()


MFA_MAX_FAILED_ATTEMPTS = 5
MFA_LOCKOUT_MINUTES = 15
MFA_RECOVERY_CODE_COUNT = 10
MFA_RECOVERY_CODE_ALPHABET = "23456789" + string.ascii_uppercase.replace(
    "O", ""
).replace("I", "")


def _mfa_max_failed_attempts(db: Session | None) -> int:
    return _setting_int(
        db,
        "mfa_max_failed_attempts",
        MFA_MAX_FAILED_ATTEMPTS,
    )


def _mfa_lockout_minutes(db: Session | None) -> int:
    return _setting_int(
        db,
        "mfa_lockout_minutes",
        MFA_LOCKOUT_MINUTES,
    )


def password_min_length(db: Session | None = None) -> int:
    return _setting_int(db, "password_min_length", 8)


# Privileged (staff/admin) accounts carry a higher password floor than the
# general minimum. Configurable via the ``system_user_password_min_length``
# auth setting; defaults to 12.
SYSTEM_USER_PASSWORD_MIN_LENGTH = 12


def password_min_length_for(db: Session | None, principal_type: str | None) -> int:
    """Minimum password length for a principal type.

    ``system_user`` (staff/admin) principals get ``max(global minimum,
    system_user floor)``; everyone else gets the global minimum.
    """
    base = password_min_length(db)
    if principal_type == "system_user":
        floor = _setting_int(
            db, "system_user_password_min_length", SYSTEM_USER_PASSWORD_MIN_LENGTH
        )
        return max(base, floor)
    return base


def ensure_mfa_not_locked(method: MFAMethod) -> None:
    locked_until = _as_utc(method.locked_until)
    if locked_until and locked_until > _now():
        raise HTTPException(
            status_code=429,
            detail=lockout_detail(
                "Too many incorrect codes", locked_until=locked_until
            ),
        )


def record_mfa_failure(db: Session, method: MFAMethod) -> None:
    method.failed_attempts = (method.failed_attempts or 0) + 1
    if method.failed_attempts >= _mfa_max_failed_attempts(db):
        method.locked_until = _now() + timedelta(minutes=_mfa_lockout_minutes(db))
        method.failed_attempts = 0
    db.commit()


def record_mfa_success(method: MFAMethod) -> None:
    method.failed_attempts = 0
    method.locked_until = None


def _normalize_recovery_code(code: str) -> str:
    return "".join(ch for ch in code.strip().upper() if ch.isalnum())


def _recovery_code_hash(code: str) -> str:
    normalized = _normalize_recovery_code(code)
    return _hash_token(f"mfa-recovery:{normalized}")


def _new_recovery_code() -> str:
    raw = "".join(secrets.choice(MFA_RECOVERY_CODE_ALPHABET) for _ in range(10))
    return f"{raw[:5]}-{raw[5:]}"


def generate_mfa_recovery_codes(
    db: Session,
    method: MFAMethod | str,
    count: int = MFA_RECOVERY_CODE_COUNT,
) -> list[str]:
    """Replace recovery codes for an MFA method and return plaintext once."""
    mfa_method = (
        method
        if isinstance(method, MFAMethod)
        else db.get(MFAMethod, coerce_uuid(method))
    )
    if not mfa_method:
        raise HTTPException(status_code=404, detail="MFA method not found")

    db.query(MFARecoveryCode).filter(
        MFARecoveryCode.mfa_method_id == mfa_method.id,
        MFARecoveryCode.is_active.is_(True),
        MFARecoveryCode.used_at.is_(None),
    ).update({"is_active": False})

    codes: list[str] = []
    seen_hashes: set[str] = set()
    for _ in range(count):
        code = _new_recovery_code()
        code_hash = _recovery_code_hash(code)
        while code_hash in seen_hashes:
            code = _new_recovery_code()
            code_hash = _recovery_code_hash(code)
        seen_hashes.add(code_hash)
        codes.append(code)
        db.add(
            MFARecoveryCode(
                mfa_method_id=mfa_method.id,
                code_hash=code_hash,
                is_active=True,
            )
        )
    db.commit()
    return codes


def _consume_mfa_recovery_code(db: Session, method: MFAMethod, code: str) -> bool:
    normalized = _normalize_recovery_code(code)
    if len(normalized) < 8:
        return False
    recovery_code = (
        db.query(MFARecoveryCode)
        .filter(MFARecoveryCode.mfa_method_id == method.id)
        .filter(MFARecoveryCode.code_hash == _recovery_code_hash(normalized))
        .filter(MFARecoveryCode.is_active.is_(True))
        .filter(MFARecoveryCode.used_at.is_(None))
        .first()
    )
    if not recovery_code:
        return False
    recovery_code.used_at = _now()
    recovery_code.is_active = False
    return True


class AuthFlow(ListResponseMixin):
    @staticmethod
    def _response_with_refresh_cookie(
        db: Session | None,
        payload: dict,
        model_cls,
        status_code: int = status.HTTP_200_OK,
    ) -> Response:
        settings = AuthFlow.refresh_cookie_settings(db)
        body_payload = {**payload, "refresh_token": None}  # nosec
        body_content = model_cls(**body_payload).model_dump_json()
        response = Response(
            content=body_content,
            status_code=status_code,
            media_type="application/json",
        )
        response.set_cookie(
            key=settings["key"],
            value=payload["refresh_token"],
            httponly=settings["httponly"],
            secure=settings["secure"],
            samesite=settings["samesite"],
            domain=settings["domain"],
            path=settings["path"],
            max_age=settings["max_age"],
        )
        return response

    @staticmethod
    def _response_clear_refresh_cookie(
        db: Session | None,
        payload: dict,
        model_cls,
        status_code: int = status.HTTP_200_OK,
    ) -> Response:
        settings = AuthFlow.refresh_cookie_settings(db)
        body_content = model_cls(**payload).model_dump_json()
        response = Response(
            content=body_content,
            status_code=status_code,
            media_type="application/json",
        )
        response.delete_cookie(
            key=settings["key"],
            domain=settings["domain"],
            path=settings["path"],
        )
        return response

    @staticmethod
    def login_response(
        db: Session,
        username: str,
        password: str,
        request: Request,
        provider: str | None,
    ):
        result = AuthFlow.login(db, username, password, request, provider)
        if result.get("refresh_token") and not _wants_refresh_in_body(request):
            return AuthFlow._response_with_refresh_cookie(
                db, result, LoginResponse, status.HTTP_200_OK
            )
        # Mobile clients (header set) receive the refresh token in the body.
        return result

    @staticmethod
    def login(
        db: Session,
        username: str,
        password: str,
        request: Request,
        provider: str | None,
    ):
        if isinstance(provider, AuthProvider):
            provider_value = provider.value
        else:
            provider_value = provider or AuthProvider.local.value
        try:
            resolved_provider = AuthProvider(provider_value)
        except ValueError as exc:
            raise HTTPException(
                status_code=400, detail="Invalid auth provider"
            ) from exc
        if resolved_provider not in (AuthProvider.radius, AuthProvider.local):
            raise HTTPException(status_code=400, detail="Unsupported auth provider")
        credential = _resolve_login_credential(
            db,
            provider=resolved_provider,
            identifier=username,
        )
        if not credential:
            raise HTTPException(status_code=401, detail="Invalid credentials")

        # Check the lock before verifying the password: a locked account must
        # answer identically to right and wrong passwords (no correctness
        # oracle), and attempts made while locked must not extend the lock.
        now = _now()
        locked_until = _as_utc(credential.locked_until)
        if locked_until and locked_until > now:
            raise HTTPException(
                status_code=403,
                detail=lockout_detail("Account locked", locked_until=locked_until),
            )
        if locked_until:
            # Lock expired: start a fresh window so a single wrong attempt
            # doesn't immediately re-lock for another full period.
            credential.failed_login_attempts = 0
            credential.locked_until = None

        if resolved_provider == AuthProvider.radius:
            try:
                radius_auth_service.authenticate(
                    db,
                    str(credential.username or username),
                    password,
                    str(credential.radius_server_id)
                    if credential.radius_server_id
                    else None,
                )
            except HTTPException as exc:
                if exc.status_code in (401, 403):
                    _record_login_failure(db, credential, now)
                raise
        else:
            if not verify_password(password, credential.password_hash):
                _record_login_failure(db, credential, now)
                raise HTTPException(status_code=401, detail="Invalid credentials")

        # Eligibility is decided BEFORE any successful-login mutation. It used to
        # run after `db.commit()` below, so a correct password against a disabled
        # principal still cleared the lockout window and stamped `last_login_at`:
        # no token was issued, but the write happened, which both corrupted
        # last-login evidence and left a credential-validity oracle in the data.
        #
        # It stays AFTER password verification deliberately. Checking eligibility
        # first would answer an unauthenticated caller differently for a disabled
        # account than for a wrong password, which is the account-state oracle the
        # lock check above is careful to avoid.
        try:
            principal_type, principal_id, principal = _principal_for_credential(
                db, credential
            )
        except staff_party_authentication.StaffProjectionError as exc:
            # Fail closed. A staff credential whose Party projection is missing,
            # conflicting or ambiguous does not authenticate — it does not fall
            # back to the legacy principal key. The refusal code is logged for
            # the operator; the caller is told only "Account disabled", so this
            # cannot be used to probe projection state.
            logger.error(
                "Staff login refused: %s (credential=%s)",
                exc.refusal.value,
                exc.credential_id,
            )
            raise HTTPException(status_code=403, detail="Account disabled") from exc
        if not principal or not getattr(principal, "is_active", False):
            raise HTTPException(status_code=403, detail="Account disabled")
        staff_binding = (
            staff_party_authentication.binding_for_principal(principal)
            if principal_type == "system_user"
            else None
        )

        if credential.must_change_password:
            raise HTTPException(
                status_code=428,
                detail={
                    "code": "PASSWORD_RESET_REQUIRED",
                    "message": "Password reset required",
                },
            )

        credential.failed_login_attempts = 0
        credential.locked_until = None
        credential.last_login_at = now
        db.commit()
        if _primary_totp_method(db, principal_type, principal_id):
            return {
                "mfa_required": True,
                "mfa_token": _issue_mfa_token(
                    db,
                    principal_id,
                    principal_type,
                    staff_binding=staff_binding,
                ),
            }
        if principal_type == "system_user" and _force_admin_mfa(db):
            return {
                "mfa_enrollment_required": True,
                "mfa_enrollment_token": _issue_mfa_enrollment_token(
                    db,
                    principal_id,
                    principal_type,
                    staff_binding=staff_binding,
                ),
            }

        return AuthFlow._issue_tokens(
            db,
            principal_type,
            principal_id,
            request,
            staff_binding=staff_binding,
        )

    @staticmethod
    def admin_mfa_setup(db: Session, system_user_id: str, label: str | None):
        system_user = cast(
            SystemUser | None, db.get(SystemUser, coerce_uuid(system_user_id))
        )
        if not system_user:
            raise HTTPException(status_code=404, detail="System user not found")

        username = system_user.email
        credential = (
            db.query(UserCredential)
            .filter(UserCredential.system_user_id == system_user.id)
            .filter(UserCredential.provider == AuthProvider.local)
            .first()
        )
        if credential and credential.username:
            username = credential.username

        secret = pyotp.random_base32()
        encrypted = _encrypt_secret(db, secret)
        # Reuse a pending (never confirmed) setup row instead of inserting a
        # new one on every visit to the setup page.
        method = (
            db.query(MFAMethod)
            .filter(MFAMethod.system_user_id == system_user.id)
            .filter(MFAMethod.method_type == MFAMethodType.totp)
            .filter(MFAMethod.enabled.is_(False))
            .filter(MFAMethod.verified_at.is_(None))
            .order_by(MFAMethod.created_at.desc())
            .first()
        )
        if method:
            method.label = label
            method.secret = encrypted
        else:
            method = MFAMethod(
                system_user_id=system_user.id,
                method_type=MFAMethodType.totp,
                label=label,
                secret=encrypted,
                enabled=False,
                is_primary=False,
            )
            db.add(method)
        db.commit()
        db.refresh(method)

        totp = pyotp.TOTP(secret)
        otpauth_uri = totp.provisioning_uri(name=username, issuer_name=_totp_issuer(db))
        return {"method_id": method.id, "secret": secret, "otpauth_uri": otpauth_uri}

    @staticmethod
    def admin_mfa_confirm(db: Session, method_id: str, code: str, system_user_id: str):
        method = db.get(MFAMethod, coerce_uuid(method_id))
        if not method:
            raise HTTPException(status_code=404, detail="MFA method not found")
        if method.subscriber_id is not None:
            raise HTTPException(status_code=403, detail="MFA method not allowed")
        if str(method.system_user_id) != str(system_user_id):
            raise HTTPException(status_code=403, detail="MFA method not allowed")
        if method.method_type != MFAMethodType.totp:
            raise HTTPException(status_code=400, detail="Unsupported MFA method")

        ensure_mfa_not_locked(method)
        secret = _decrypt_secret(db, method.secret or "")
        totp = pyotp.TOTP(secret)
        if not totp.verify(code, valid_window=0):
            record_mfa_failure(db, method)
            raise HTTPException(status_code=401, detail="Invalid MFA code")
        record_mfa_success(method)

        db.query(MFAMethod).filter(
            MFAMethod.system_user_id == method.system_user_id,
            MFAMethod.id != method.id,
            MFAMethod.is_primary.is_(True),
        ).update({"is_primary": False})

        method.enabled = True
        method.is_primary = True
        method.is_active = True
        method.verified_at = _now()
        try:
            db.commit()
        except IntegrityError as exc:
            db.rollback()
            raise HTTPException(
                status_code=409,
                detail="Primary MFA method already exists for this user",
            ) from exc
        db.refresh(method)
        return method

    @staticmethod
    def reseller_mfa_setup(db: Session, reseller_user_id: str, label: str | None):
        """TOTP enrolment for a first-class reseller_user principal (Layer 3)."""
        reseller_user = db.get(ResellerUser, coerce_uuid(reseller_user_id))
        if not reseller_user:
            raise HTTPException(status_code=404, detail="Reseller user not found")

        username = reseller_user.email or str(reseller_user.id)
        credential = (
            db.query(UserCredential)
            .filter(UserCredential.reseller_user_id == reseller_user.id)
            .filter(UserCredential.provider == AuthProvider.local)
            .first()
        )
        if credential and credential.username:
            username = credential.username

        secret = pyotp.random_base32()
        encrypted = _encrypt_secret(db, secret)
        method = (
            db.query(MFAMethod)
            .filter(MFAMethod.reseller_user_id == reseller_user.id)
            .filter(MFAMethod.method_type == MFAMethodType.totp)
            .filter(MFAMethod.enabled.is_(False))
            .filter(MFAMethod.verified_at.is_(None))
            .order_by(MFAMethod.created_at.desc())
            .first()
        )
        if method:
            method.label = label
            method.secret = encrypted
        else:
            method = MFAMethod(
                reseller_user_id=reseller_user.id,
                method_type=MFAMethodType.totp,
                label=label,
                secret=encrypted,
                enabled=False,
                is_primary=False,
            )
            db.add(method)
        db.commit()
        db.refresh(method)

        totp = pyotp.TOTP(secret)
        otpauth_uri = totp.provisioning_uri(name=username, issuer_name=_totp_issuer(db))
        return {"method_id": method.id, "secret": secret, "otpauth_uri": otpauth_uri}

    @staticmethod
    def reseller_mfa_confirm(
        db: Session, method_id: str, code: str, reseller_user_id: str
    ):
        method = db.get(MFAMethod, coerce_uuid(method_id))
        if not method:
            raise HTTPException(status_code=404, detail="MFA method not found")
        if str(method.reseller_user_id) != str(reseller_user_id):
            raise HTTPException(status_code=403, detail="MFA method not allowed")
        if method.method_type != MFAMethodType.totp:
            raise HTTPException(status_code=400, detail="Unsupported MFA method")

        ensure_mfa_not_locked(method)
        secret = _decrypt_secret(db, method.secret or "")
        totp = pyotp.TOTP(secret)
        if not totp.verify(code, valid_window=0):
            record_mfa_failure(db, method)
            raise HTTPException(status_code=401, detail="Invalid MFA code")
        record_mfa_success(method)

        db.query(MFAMethod).filter(
            MFAMethod.reseller_user_id == method.reseller_user_id,
            MFAMethod.id != method.id,
            MFAMethod.is_primary.is_(True),
        ).update({"is_primary": False})

        method.enabled = True
        method.is_primary = True
        method.is_active = True
        method.verified_at = _now()
        try:
            db.commit()
        except IntegrityError as exc:
            db.rollback()
            raise HTTPException(
                status_code=409,
                detail="Primary MFA method already exists for this user",
            ) from exc
        db.refresh(method)
        return method

    @staticmethod
    def mfa_setup(db: Session, subscriber_id: str, label: str | None):
        subscriber = _subscriber_or_404(db, subscriber_id)
        username = subscriber.email
        credential = (
            db.query(UserCredential)
            .filter(UserCredential.subscriber_id == subscriber.id)
            .filter(UserCredential.provider == AuthProvider.local)
            .first()
        )
        if credential and credential.username:
            username = credential.username

        secret = pyotp.random_base32()
        encrypted = _encrypt_secret(db, secret)
        # Reuse a pending (never confirmed) setup row instead of inserting a
        # new one on every visit to the setup page.
        method = (
            db.query(MFAMethod)
            .filter(MFAMethod.subscriber_id == subscriber.id)
            .filter(MFAMethod.method_type == MFAMethodType.totp)
            .filter(MFAMethod.enabled.is_(False))
            .filter(MFAMethod.verified_at.is_(None))
            .order_by(MFAMethod.created_at.desc())
            .first()
        )
        if method:
            method.label = label
            method.secret = encrypted
        else:
            method = MFAMethod(
                subscriber_id=subscriber.id,
                method_type=MFAMethodType.totp,
                label=label,
                secret=encrypted,
                enabled=False,
                is_primary=False,
            )
            db.add(method)
        db.commit()
        db.refresh(method)

        totp = pyotp.TOTP(secret)
        otpauth_uri = totp.provisioning_uri(name=username, issuer_name=_totp_issuer(db))
        return {"method_id": method.id, "secret": secret, "otpauth_uri": otpauth_uri}

    @staticmethod
    def mfa_confirm(db: Session, method_id: str, code: str, subscriber_id: str):
        method = db.get(MFAMethod, coerce_uuid(method_id))
        if not method:
            raise HTTPException(status_code=404, detail="MFA method not found")
        if str(method.subscriber_id) != str(subscriber_id):
            raise HTTPException(status_code=403, detail="MFA method not allowed")
        if method.method_type != MFAMethodType.totp:
            raise HTTPException(status_code=400, detail="Unsupported MFA method")

        ensure_mfa_not_locked(method)
        secret = _decrypt_secret(db, method.secret or "")
        totp = pyotp.TOTP(secret)
        if not totp.verify(code, valid_window=0):
            record_mfa_failure(db, method)
            raise HTTPException(status_code=401, detail="Invalid MFA code")
        record_mfa_success(method)

        db.query(MFAMethod).filter(
            MFAMethod.subscriber_id == method.subscriber_id,
            MFAMethod.id != method.id,
            MFAMethod.is_primary.is_(True),
        ).update({"is_primary": False})

        method.enabled = True
        method.is_primary = True
        method.is_active = True
        method.verified_at = _now()
        try:
            db.commit()
        except IntegrityError as exc:
            db.rollback()
            raise HTTPException(
                status_code=409,
                detail="Primary MFA method already exists for this user",
            ) from exc
        db.refresh(method)
        return method

    @staticmethod
    def mfa_verify(db: Session, mfa_token: str, code: str, request: Request):
        payload = _decode_jwt(db, mfa_token, "mfa")
        principal_id = payload.get("principal_id") or payload.get("sub")
        principal_type = payload.get("principal_type") or "subscriber"
        if not principal_id:
            raise HTTPException(status_code=401, detail="Invalid MFA token")
        staff_binding: staff_party_authentication.StaffSessionBinding | None = None
        if principal_type == "system_user":
            staff_binding = staff_binding_from_token_payload(
                payload,
                invalid_detail="Invalid MFA token",
            )
            try:
                principal = staff_party_authentication.resolve_staff_principal_by_party(
                    db,
                    staff_binding.party_id,
                    staff_binding.system_user_id,
                    reference=staff_binding.system_user_id,
                )
            except staff_party_authentication.StaffProjectionError as exc:
                logger.error(
                    "Staff MFA verification refused: %s (subject=%s)",
                    exc.refusal.value,
                    exc.credential_id,
                )
                raise HTTPException(
                    status_code=401,
                    detail="Invalid MFA token",
                ) from exc
            principal_id = str(principal.id)

        method = _primary_totp_method(db, principal_type, str(principal_id))
        if not method:
            raise HTTPException(status_code=404, detail="MFA method not found")

        ensure_mfa_not_locked(method)
        secret = _decrypt_secret(db, method.secret or "")
        totp = pyotp.TOTP(secret)
        if totp.verify(code, valid_window=0):
            record_mfa_success(method)
        elif _consume_mfa_recovery_code(db, method, code):
            record_mfa_success(method)
        else:
            record_mfa_failure(db, method)
            raise HTTPException(status_code=401, detail="Invalid MFA code")

        method.last_used_at = _now()
        db.commit()
        return AuthFlow._issue_tokens(
            db,
            principal_type,
            str(principal_id),
            request,
            staff_binding=staff_binding,
        )

    @staticmethod
    def mfa_verify_response(db: Session, mfa_token: str, code: str, request: Request):
        result = AuthFlow.mfa_verify(db, mfa_token, code, request)
        if _wants_refresh_in_body(request):
            return result
        return AuthFlow._response_with_refresh_cookie(
            db, result, TokenResponse, status.HTTP_200_OK
        )

    @staticmethod
    def refresh(db: Session, refresh_token: str, request: Request):
        token_hash = _hash_token(refresh_token)
        session = (
            db.query(AuthSession)
            .filter(AuthSession.token_hash == token_hash)
            .filter(AuthSession.status == SessionStatus.active)
            .filter(AuthSession.revoked_at.is_(None))
            .first()
        )
        if not session:
            reused = (
                db.query(AuthSession)
                .filter(AuthSession.previous_token_hash == token_hash)
                .filter(AuthSession.status == SessionStatus.active)
                .filter(AuthSession.revoked_at.is_(None))
                .first()
            )
            if reused:
                reused.status = SessionStatus.revoked
                reused.revoked_at = _now()
                db.commit()
                raise HTTPException(
                    status_code=401,
                    detail="Refresh token reuse detected",
                )
            raise HTTPException(status_code=401, detail="Invalid refresh token")
        expires_at = _as_utc(session.expires_at)
        if expires_at and expires_at <= _now():
            session.status = SessionStatus.expired
            db.commit()
            raise HTTPException(status_code=401, detail="Refresh token expired")

        principal_type, principal_id = principal_from_session(session)
        if principal_type == "system_user":
            # Resolve identity BEFORE rotating the refresh token. A projection
            # refusal is an authorization failure and must leave the session
            # byte-for-byte unchanged rather than consuming the caller's token.
            try:
                principal = staff_party_authentication.resolve_staff_session_principal(
                    db,
                    party_id=session.party_id,
                    system_user_id=session.system_user_id,
                    reference=str(session.id),
                )
            except staff_party_authentication.StaffProjectionError as exc:
                logger.error(
                    "Staff refresh refused: %s (subject=%s)",
                    exc.refusal.value,
                    exc.credential_id,
                )
                raise HTTPException(
                    status_code=401,
                    detail="Invalid refresh token",
                ) from exc
            principal_id = str(principal.id)

        new_refresh = secrets.token_urlsafe(48)
        session.previous_token_hash = session.token_hash
        session.token_hash = _hash_token(new_refresh)
        session.token_rotated_at = _now()
        session.last_seen_at = _now()
        if request.client:
            session.ip_address = request.client.host
        session.user_agent = _truncate_user_agent(request.headers.get("user-agent"))
        db.commit()
        access_token = _issue_access_token(
            db, principal_id, principal_type, str(session.id)
        )
        return {"access_token": access_token, "refresh_token": new_refresh}

    @staticmethod
    def refresh_response(db: Session, refresh_token: str | None, request: Request):
        resolved = AuthFlow.resolve_refresh_token(request, refresh_token, db)
        if not resolved:
            raise HTTPException(status_code=401, detail="Missing refresh token")
        result = AuthFlow.refresh(db, resolved, request)
        if _wants_refresh_in_body(request):
            return result
        return AuthFlow._response_with_refresh_cookie(
            db, result, TokenResponse, status.HTTP_200_OK
        )

    @staticmethod
    def logout(db: Session, refresh_token: str):
        token_hash = _hash_token(refresh_token)
        session = (
            db.query(AuthSession)
            .filter(AuthSession.token_hash == token_hash)
            .filter(AuthSession.revoked_at.is_(None))
            .first()
        )
        if not session:
            raise HTTPException(status_code=404, detail="Session not found")
        principal_type = "system_user" if session.system_user_id else "subscriber"
        principal_id = str(session.system_user_id or session.subscriber_id)
        session.status = SessionStatus.revoked
        session.revoked_at = _now()
        db.commit()
        auth_cache.invalidate_session_context(
            str(session.id),
            principal_type=principal_type,
            principal_id=principal_id,
        )
        return {"revoked_at": session.revoked_at}

    @staticmethod
    def logout_response(db: Session, refresh_token: str | None, request: Request):
        resolved = AuthFlow.resolve_refresh_token(request, refresh_token, db)
        if not resolved:
            raise HTTPException(status_code=404, detail="Session not found")
        result = AuthFlow.logout(db, resolved)
        return AuthFlow._response_clear_refresh_cookie(
            db, result, LogoutResponse, status.HTTP_200_OK
        )

    @staticmethod
    def resolve_refresh_token(
        request: Request, refresh_token: str | None, db: Session | None = None
    ):
        settings = AuthFlow.refresh_cookie_settings(db)
        return refresh_token or request.cookies.get(settings["key"])

    @staticmethod
    def refresh_cookie_settings(db: Session | None = None):
        return {
            "key": _refresh_cookie_name(db),
            "httponly": True,
            "secure": _refresh_cookie_secure(db),
            "samesite": _refresh_cookie_samesite(db),
            "domain": _refresh_cookie_domain(db),
            "path": _refresh_cookie_path(db),
            "max_age": _refresh_ttl_days(db) * 24 * 60 * 60,
        }

    @staticmethod
    def _issue_tokens(
        db: Session,
        principal_type_or_principal_id: str,
        principal_id_or_request: str | Request,
        request: Request | None = None,
        *,
        staff_binding: staff_party_authentication.StaffSessionBinding | None = None,
    ):
        # Backward compatibility: older callers passed (db, principal_id, request)
        # and implicitly targeted subscriber principals.
        if request is None:
            principal_type = "subscriber"
            principal_id = principal_type_or_principal_id
            active_request = cast(Request, principal_id_or_request)
        else:
            principal_type = principal_type_or_principal_id
            principal_id = cast(str, principal_id_or_request)
            active_request = request

        principal_uuid = coerce_uuid(principal_id)
        if principal_type == "system_user":
            if staff_binding is None:
                raise staff_party_authentication.StaffProjectionError(
                    staff_party_authentication.StaffProjectionRefusal.projection_missing,
                    principal_uuid,
                )
            # Validate the Party-keyed identity/context pair before revoking a
            # prior device session or performing any other write.
            staff_principal = (
                staff_party_authentication.resolve_staff_principal_by_party(
                    db,
                    staff_binding.party_id,
                    staff_binding.system_user_id,
                    reference=principal_uuid,
                )
            )
            if staff_principal.id != principal_uuid:
                raise staff_party_authentication.StaffProjectionError(
                    staff_party_authentication.StaffProjectionRefusal.projection_conflict,
                    principal_uuid,
                )
        refresh_token = secrets.token_urlsafe(48)
        now = _now()
        expires_at = now + timedelta(days=_refresh_ttl_days(db))
        device_id = _clean_device_id(active_request.headers.get("x-device-id"))
        session_kwargs = dict(
            status=SessionStatus.active,
            token_hash=_hash_token(refresh_token),
            ip_address=client_ip(active_request),
            user_agent=_truncate_user_agent(active_request.headers.get("user-agent")),
            device_id=device_id,
            created_at=now,
            last_seen_at=now,
            expires_at=expires_at,
        )
        principal_column = {
            "system_user": AuthSession.system_user_id,
            "reseller_user": AuthSession.reseller_user_id,
        }.get(principal_type, AuthSession.subscriber_id)
        # Per-device replace: a re-login from a known device supersedes that
        # device's prior active session instead of stacking a new row. Revoked in
        # the same transaction as the insert, so the principal always has at most
        # one active session per device.
        if device_id:
            db.query(AuthSession).filter(
                principal_column == principal_uuid,
                AuthSession.device_id == device_id,
                AuthSession.status == SessionStatus.active,
                AuthSession.revoked_at.is_(None),
            ).update(
                {
                    AuthSession.status: SessionStatus.revoked,
                    AuthSession.revoked_at: now,
                },
                synchronize_session=False,
            )
        if principal_type == "system_user":
            # Write BOTH halves of the bound pair. `party_id` is the identity
            # the later ratchet will validate from; `system_user_id` stays as
            # the Sub-owned staff context and is not being retired. The typed
            # binding was resolved from Party before any mutation above.
            assert staff_binding is not None
            session = AuthSession(
                system_user_id=principal_uuid,
                party_id=staff_binding.party_id,
                **session_kwargs,
            )
        elif principal_type == "reseller_user":
            session = AuthSession(reseller_user_id=principal_uuid, **session_kwargs)
        else:
            session = AuthSession(subscriber_id=principal_uuid, **session_kwargs)
        db.add(session)
        db.commit()
        db.refresh(session)
        access_token = _issue_access_token(
            db, str(principal_uuid), principal_type, str(session.id)
        )
        return {"access_token": access_token, "refresh_token": refresh_token}


auth_flow = AuthFlow()


def change_password(
    db: Session,
    subscriber_id: str,
    current_password: str,
    new_password: str,
    *,
    current_session_id: str | None = None,
) -> datetime:
    """
    Change a user's password after verifying the current password.
    Revokes every other session for the principal and returns the timestamp
    when the password was changed.
    """
    principal_uuid = coerce_uuid(subscriber_id)
    stmt = (
        sa_select(UserCredential)
        .where(
            (UserCredential.subscriber_id == principal_uuid)
            | (UserCredential.system_user_id == principal_uuid)
            | (UserCredential.reseller_user_id == principal_uuid)
        )
        .where(UserCredential.provider == AuthProvider.local)
        .where(UserCredential.is_active.is_(True))
    )
    credential = db.scalars(stmt).first()

    if not credential:
        raise HTTPException(status_code=404, detail="No credentials found")

    if not verify_password(current_password, credential.password_hash):
        raise HTTPException(status_code=401, detail="Current password is incorrect")

    if current_password == new_password:
        raise HTTPException(status_code=400, detail="New password must be different")

    now = _now()
    credential.password_hash = hash_password(new_password)
    credential.password_updated_at = now
    credential.must_change_password = False

    if credential.system_user_id is not None:
        principal_type = "system_user"
        session_principal_filter = (
            AuthSession.system_user_id == credential.system_user_id
        )
    elif getattr(credential, "reseller_user_id", None) is not None:
        principal_type = "reseller_user"
        session_principal_filter = (
            AuthSession.reseller_user_id == credential.reseller_user_id
        )
    else:
        principal_type = "subscriber"
        session_principal_filter = AuthSession.subscriber_id == credential.subscriber_id
    revoke_query = (
        db.query(AuthSession)
        .filter(session_principal_filter)
        .filter(AuthSession.status == SessionStatus.active)
        .filter(AuthSession.revoked_at.is_(None))
    )
    if current_session_id:
        revoke_query = revoke_query.filter(AuthSession.id != current_session_id)
    revoked = revoke_query.all()
    for session in revoked:
        session.status = SessionStatus.revoked
        session.revoked_at = now
    db.commit()
    for session in revoked:
        auth_cache.invalidate_session_context(
            str(session.id),
            principal_type=principal_type,
            principal_id=str(principal_uuid),
        )
    if principal_type == "subscriber":
        _revoke_portal_sessions_for_subscriber(db, str(credential.subscriber_id))

    return now


def _revoke_portal_sessions_for_subscriber(db: Session, subscriber_id: str) -> None:
    """Best-effort: drop Redis-backed customer/reseller web portal sessions too.

    `auth_sessions` revocation does not touch the opaque-token portal sessions,
    so a password change/reset would otherwise leave logged-in portal browsers
    untouched until their (sliding) TTL lapsed.
    """
    # Local imports: reseller_portal imports this module at import time.
    from app.services import customer_portal_session, reseller_portal

    try:
        customer_portal_session.revoke_customer_sessions_for_subscriber(
            subscriber_id, db=db
        )
        reseller_portal.revoke_reseller_sessions_for_subscriber(subscriber_id, db=db)
    except Exception:
        logger.warning(
            "Failed to revoke portal sessions for subscriber %s",
            subscriber_id,
            exc_info=True,
        )


def forgot_password_flow(
    db: Session, email: str, *, next_login_path: str | None = None
) -> None:
    """Deprecated compatibility adapter for durable password recovery."""

    from app.services import credential_recovery
    from app.services.owner_commands import CommandContext

    credential_recovery.request_password_recovery(
        db,
        credential_recovery.RequestPasswordRecoveryCommand(
            context=CommandContext.system(
                actor="service:legacy-auth-flow",
                scope=credential_recovery.CREDENTIAL_RECOVERY_SCOPE,
                reason="Legacy password recovery compatibility request",
            ),
            email=email,
            next_login_path=next_login_path,
        ),
    )


def _password_reset_result(capability) -> dict | None:
    if capability is None:
        return None
    return {
        "token": capability.token,
        "email": capability.email,
        "subscriber_name": capability.person_name,
        "principal_type": capability.principal_type,
        "principal_id": str(capability.principal_id),
        "ttl_minutes": capability.ttl_minutes,
    }


def request_password_reset(
    db: Session, email: str, *, ttl_minutes: int | None = None
) -> dict | None:
    """Deprecated token-only compatibility lookup for test and forced-reset code."""

    from app.services import credential_recovery

    return _password_reset_result(
        credential_recovery.issue_reset_capability_for_email(
            db,
            email,
            ttl_minutes=ttl_minutes,
        )
    )


def request_principal_password_reset(
    db: Session,
    *,
    principal_type: str,
    principal_id: UUID,
    ttl_minutes: int | None = None,
) -> dict | None:
    """Deprecated exact-capability compatibility wrapper."""

    from app.services import credential_recovery

    return _password_reset_result(
        credential_recovery.issue_exact_reset_capability(
            db,
            principal_type=principal_type,
            principal_id=principal_id,
            ttl_minutes=ttl_minutes,
        )
    )


def request_system_user_password_reset(
    db: Session,
    system_user_id: UUID,
    *,
    ttl_minutes: int | None = None,
) -> dict | None:
    """Deprecated exact staff-capability compatibility wrapper."""

    return request_principal_password_reset(
        db,
        principal_type="system_user",
        principal_id=system_user_id,
        ttl_minutes=ttl_minutes,
    )


def reset_password(db: Session, token: str, new_password: str) -> datetime:
    """Deprecated HTTP compatibility adapter for the contracted reset owner."""

    from app.services import credential_recovery
    from app.services.domain_errors import DomainError
    from app.services.owner_commands import CommandContext

    compatibility_db = Session(
        bind=db.connection(),
        autoflush=False,
        join_transaction_mode="create_savepoint",
    )
    try:
        outcome = credential_recovery.complete_password_reset(
            compatibility_db,
            credential_recovery.CompletePasswordResetCommand(
                context=CommandContext.system(
                    actor="service:legacy-auth-flow",
                    scope=credential_recovery.CREDENTIAL_RECOVERY_SCOPE,
                    reason="Legacy password reset compatibility redemption",
                ),
                token=token,
                new_password=new_password,
            ),
        )
    except DomainError as exc:
        status_code = {
            "auth.credential_recovery.invalid_password": 400,
            "auth.credential_recovery.invalid_reset_capability": 401,
            "auth.credential_recovery.credential_not_found": 404,
        }.get(exc.code, 500)
        raise HTTPException(
            status_code=status_code,
            detail=exc.message.rstrip("."),
        ) from exc
    finally:
        compatibility_db.close()
        db.expire_all()
    return outcome.reset_at


def send_email_verification(db: Session, subscriber_id: str) -> bool:
    """
    Mint an email-verification token and send the verification email to the
    subscriber's address. No-op (returns False) when the subscriber is missing,
    has no email, or is already verified.

    Returns True if a verification email was dispatched.
    """
    from app.models.audit import AuditActorType
    from app.services.audit_adapter import record_audit_event
    from app.services.email import send_email_verification_email

    subscriber = cast(Subscriber | None, db.get(Subscriber, coerce_uuid(subscriber_id)))
    if not subscriber or not subscriber.email:
        return False
    if subscriber.email_verified:
        # Already verified: nothing to send.
        return False

    # Resolve the one authorised address before minting anything: a capability
    # that cannot be delivered to exactly one mailbox should not exist.
    recipient = resolve_capability_recipient(
        subscriber.email, subject=f"subscriber:{subscriber.id}"
    )
    ttl_minutes = _email_verification_ttl_minutes(db)
    token = _issue_email_verification_token(
        db,
        str(subscriber.id),
        recipient,
        ttl_minutes=ttl_minutes,
    )
    record_audit_event(
        db,
        action="auth.email_verification_requested",
        entity_type="subscriber",
        entity_id=str(subscriber.id),
        actor_type=AuditActorType.user,
        actor_id=str(subscriber.id),
        metadata={"email": recipient},
    )
    return send_email_verification_email(
        db=db,
        to_email=recipient,
        verification_token=token,
        person_name=subscriber.display_name or subscriber.first_name,
        expires_minutes=ttl_minutes,
    )


def verify_email(db: Session, token: str) -> Subscriber:
    """
    Verify a subscriber's email using a valid verification token.

    Idempotent: an already-verified subscriber is a success no-op. Raises
    HTTPException on an invalid/expired token or mismatched subscriber.
    Returns the (verified) Subscriber.
    """
    from app.models.audit import AuditActorType
    from app.services.audit_adapter import record_audit_event

    payload = _decode_email_verification_token(db, token)
    principal_id = payload.get("principal_id") or payload.get("sub")
    email = payload.get("email")
    if not principal_id or not email:
        raise HTTPException(status_code=401, detail="Invalid verification token")

    subscriber = cast(Subscriber | None, db.get(Subscriber, coerce_uuid(principal_id)))
    if not subscriber or subscriber.email != email:
        raise HTTPException(status_code=401, detail="Invalid verification token")

    if subscriber.email_verified:
        return subscriber

    subscriber.email_verified = True
    record_audit_event(
        db,
        action="auth.email_verified",
        entity_type="subscriber",
        entity_id=str(subscriber.id),
        actor_type=AuditActorType.user,
        actor_id=str(subscriber.id),
        metadata={"email": subscriber.email},
        defer_until_commit=True,
    )
    db.commit()
    db.refresh(subscriber)
    return subscriber


def set_subscriber_email(
    db: Session, subscriber_id: str, new_email: str | None
) -> bool:
    """Add or change a subscriber's email, re-arming verification.

    Single source of truth shared by the web profile form and the mobile ``/me``
    update so the rule "a changed/added email must be re-verified, with a fresh
    link dispatched" lives in exactly one place. No-op (returns ``False``) when
    the email is blank or unchanged. Returns ``True`` when the email changed (and
    a verification email was dispatched). Email is non-unique contact info, so
    sharing an address with another subscriber is allowed and not blocked.
    """
    new_email = (new_email or "").strip()
    if not new_email:
        return False
    subscriber = cast(Subscriber | None, db.get(Subscriber, coerce_uuid(subscriber_id)))
    if subscriber is None:
        return False
    if new_email.lower() == (subscriber.email or "").strip().lower():
        return False

    from app.schemas.subscriber import SubscriberUpdate
    from app.services import subscriber as subscriber_service

    # Route through the subscriber service so the unique constraint and the
    # customer identity-resolution index are maintained the same way the web
    # and admin edit paths do.
    subscriber_service.subscribers.update(
        db,
        str(subscriber.id),
        SubscriberUpdate(email=new_email, email_verified=False),
    )
    try:
        send_email_verification(db, str(subscriber.id))
    except Exception:
        logger.warning(
            "verification email after email change failed for %s",
            subscriber_id,
            exc_info=True,
        )
    return True


def validate_active_session(
    db: Session,
    session_id: str,
    principal_id: str,
) -> tuple[AuthSession, object, str] | None:
    """Validate that an active, non-expired session exists for the subscriber.

    Returns (session, subscriber) tuple if valid, else None.
    """
    now = _now()
    session = (
        db.query(AuthSession)
        .filter(AuthSession.id == session_id)
        .filter(AuthSession.status == SessionStatus.active)
        .filter(AuthSession.revoked_at.is_(None))
        .filter(AuthSession.expires_at > now)
        .first()
    )
    if not session:
        return None
    principal_type = "system_user" if session.system_user_id else "subscriber"
    active_id = str(session.system_user_id or session.subscriber_id)
    if active_id != str(principal_id):
        return None

    if principal_type == "system_user":
        # Two branches, and the split is the cutover:
        #
        #   party_id present -> Party-KEYED through the canonical primitive,
        #                       with system_user_id compared as the Sub-context
        #                       assertion. This is the destination.
        #   party_id absent  -> the named assertion-first bridge, for sessions
        #                       predating migration 534 only. Deploy 2 deletes
        #                       this branch and requires party_id.
        #
        # Returning None is the fail-closed answer — the request is
        # unauthenticated and the caller learns nothing about why.
        try:
            principal = staff_party_authentication.resolve_staff_session_principal(
                db,
                party_id=session.party_id,
                system_user_id=session.system_user_id,
                reference=str(session.id),
            )
        except staff_party_authentication.StaffProjectionError as exc:
            logger.error(
                "Staff session refused: %s (system_user=%s)",
                exc.refusal.value,
                exc.credential_id,
            )
            return None
    else:
        principal = db.get(Subscriber, active_id)
    if not principal:
        return None

    return session, principal, principal_type
