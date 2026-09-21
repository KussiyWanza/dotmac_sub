"""Shared, transport-neutral JWT signing policy and codec."""

from __future__ import annotations

import os
import warnings
from datetime import datetime, timedelta
from typing import cast

from dotmac_kernel.secret_sources import get_secret as held_secret
from jose import jwt
from sqlalchemy.orm import Session

from app.models.domain_settings import DomainSetting, SettingDomain
from app.services.secrets import resolve_secret


class TokenSigningConfigurationError(RuntimeError):
    """Raised when the process has no held JWT signing key."""


def env_value(name: str) -> str | None:
    value = os.getenv(name)
    if value is None or value == "":
        return None
    return value


def env_int(name: str) -> int | None:
    raw = env_value(name)
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def setting_value(db: Session | None, key: str) -> str | None:
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


def jwt_secret(db: Session | None) -> str:
    secret = resolve_secret(env_value("JWT_SECRET")) or held_secret("jwt_secret")
    if not secret:
        raise TokenSigningConfigurationError("JWT secret is not configured")
    return secret


def jwt_algorithm(db: Session | None) -> str:
    return env_value("JWT_ALGORITHM") or setting_value(db, "jwt_algorithm") or "HS256"


def access_ttl_minutes(db: Session | None) -> int:
    configured = env_int("JWT_ACCESS_TTL_MINUTES")
    if configured is not None:
        return configured
    value = setting_value(db, "jwt_access_ttl_minutes")
    if value is not None:
        try:
            return int(value)
        except ValueError:
            pass
    return 15


def encode_access_token(
    *,
    principal_id: str,
    principal_type: str,
    session_id: str,
    issued_at: datetime,
    ttl_minutes: int,
    secret: str,
    algorithm: str,
    roles: list[str] | None = None,
    permissions: list[str] | None = None,
) -> str:
    payload: dict[str, object] = {
        "sub": principal_id,
        "principal_id": principal_id,
        "principal_type": principal_type,
        "session_id": session_id,
        "typ": "access",
        "iat": int(issued_at.timestamp()),
        "exp": int((issued_at + timedelta(minutes=ttl_minutes)).timestamp()),
    }
    if roles:
        payload["roles"] = roles
    if permissions:
        payload["scopes"] = permissions
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore",
            message=r"datetime\.datetime\.utcnow\(\) is deprecated.*",
            category=DeprecationWarning,
            module=r"jose\.jwt",
        )
        return cast(str, jwt.encode(payload, secret, algorithm=algorithm))


def issue_access_token(
    db: Session | None,
    *,
    principal_id: str,
    principal_type: str,
    session_id: str,
    issued_at: datetime,
    roles: list[str] | None = None,
    permissions: list[str] | None = None,
) -> str:
    return encode_access_token(
        principal_id=principal_id,
        principal_type=principal_type,
        session_id=session_id,
        issued_at=issued_at,
        ttl_minutes=access_ttl_minutes(db),
        secret=jwt_secret(db),
        algorithm=jwt_algorithm(db),
        roles=roles,
        permissions=permissions,
    )
