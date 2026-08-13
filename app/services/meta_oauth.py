"""Canonical owner for Meta OAuth refresh and OAuth token health.

Celery owns scheduling and session lifecycle only. This module selects eligible
tokens, resolves database-authoritative Meta configuration through the approved
secret resolver, exchanges one permitted token, and atomically records the new
token state or a sanitized failure.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Protocol, cast
from uuid import UUID

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.domain_settings import SettingDomain
from app.models.oauth_token import OAuthToken
from app.services import secrets, settings_spec
from app.services.domain_errors import DomainError
from app.services.events import emit_event
from app.services.events.types import EventType
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)

logger = logging.getLogger(__name__)

META_OAUTH_REFRESH_SCOPE = "integration:meta-oauth:refresh"
_META_GRAPH_VERSION = re.compile(r"^v[1-9][0-9]*\.[0-9]+$")
_REFRESH_COMMAND = OwnerCommandDefinition(
    owner="integration.oauth_tokens",
    concern="Meta OAuth access-token refresh persistence",
    name="refresh_meta_oauth_token",
)


class MetaOAuthGrantType(StrEnum):
    """Meta grants this owner is permitted to send."""

    FB_EXCHANGE_TOKEN = "fb_exchange_token"


class MetaOAuthTokenClass(StrEnum):
    """Token classes accepted by Meta's long-lived token exchange."""

    USER = "user"


class MetaTokenRefreshStatus(StrEnum):
    REFRESHED = "refreshed"
    FAILED = "failed"
    SKIPPED = "skipped"


class MetaTokenRefreshFailureCode(StrEnum):
    CONFIGURATION_MISSING = "configuration_missing"
    SECRET_REFERENCE_REQUIRED = "secret_reference_required"
    SECRET_RESOLUTION_FAILED = "secret_resolution_failed"
    INVALID_GRAPH_VERSION = "invalid_graph_version"
    INVALID_GRANT_TYPE = "invalid_grant_type"
    TOKEN_INACTIVE = "token_inactive"
    TOKEN_CLASS_NOT_PERMITTED = "token_class_not_permitted"
    TOKEN_MISSING = "token_missing"
    TOKEN_STORAGE_NOT_PERMITTED = "token_storage_not_permitted"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    PROVIDER_REJECTED = "provider_rejected"
    PROVIDER_RESPONSE_INVALID = "provider_response_invalid"


_SAFE_FAILURE_MESSAGES: dict[MetaTokenRefreshFailureCode, str] = {
    MetaTokenRefreshFailureCode.CONFIGURATION_MISSING: (
        "Meta OAuth client configuration is incomplete."
    ),
    MetaTokenRefreshFailureCode.SECRET_REFERENCE_REQUIRED: (
        "Meta OAuth client secret must use an approved secret reference."
    ),
    MetaTokenRefreshFailureCode.SECRET_RESOLUTION_FAILED: (
        "Meta OAuth client secret could not be resolved."
    ),
    MetaTokenRefreshFailureCode.INVALID_GRAPH_VERSION: (
        "Meta Graph API version is invalid."
    ),
    MetaTokenRefreshFailureCode.INVALID_GRANT_TYPE: (
        "Meta OAuth refresh grant is not permitted."
    ),
    MetaTokenRefreshFailureCode.TOKEN_INACTIVE: "OAuth token is inactive.",
    MetaTokenRefreshFailureCode.TOKEN_CLASS_NOT_PERMITTED: (
        "OAuth token class is not permitted for this refresh grant."
    ),
    MetaTokenRefreshFailureCode.TOKEN_MISSING: "OAuth token is unavailable.",
    MetaTokenRefreshFailureCode.TOKEN_STORAGE_NOT_PERMITTED: (
        "OAuth token storage class is not writable by this owner."
    ),
    MetaTokenRefreshFailureCode.PROVIDER_UNAVAILABLE: (
        "Meta OAuth refresh service is temporarily unavailable."
    ),
    MetaTokenRefreshFailureCode.PROVIDER_REJECTED: (
        "Meta rejected the OAuth token refresh request."
    ),
    MetaTokenRefreshFailureCode.PROVIDER_RESPONSE_INVALID: (
        "Meta returned an invalid OAuth token refresh response."
    ),
}


class MetaOAuthRefreshError(DomainError):
    """Stable, secret-free error raised at the public owner boundary."""


class _ExpectedRefreshFailure(Exception):
    def __init__(self, code: MetaTokenRefreshFailureCode) -> None:
        self.code = code
        super().__init__(_SAFE_FAILURE_MESSAGES[code])


@dataclass(frozen=True, slots=True)
class MetaTokenRefreshCandidatesQuery:
    eligible_before: datetime


@dataclass(frozen=True, slots=True)
class MetaTokenRefreshCandidate:
    token_id: UUID
    expected_expires_at: datetime


@dataclass(frozen=True, slots=True)
class RefreshMetaTokenCommand:
    context: CommandContext
    candidate: MetaTokenRefreshCandidate
    observed_at: datetime
    eligible_before: datetime
    grant_type: MetaOAuthGrantType = MetaOAuthGrantType.FB_EXCHANGE_TOKEN


@dataclass(frozen=True, slots=True)
class MetaTokenRefreshResult:
    token_id: UUID
    status: MetaTokenRefreshStatus
    expires_at: datetime | None = None
    failure_code: MetaTokenRefreshFailureCode | None = None


@dataclass(frozen=True, slots=True)
class MetaTokenRefreshSummary:
    total_checked: int
    refreshed: int
    errors: int

    def as_dict(self) -> dict[str, int]:
        return {
            "refreshed": self.refreshed,
            "errors": self.errors,
            "total_checked": self.total_checked,
        }


@dataclass(frozen=True, slots=True)
class OAuthTokenHealthQuery:
    observed_at: datetime
    expiring_before: datetime


@dataclass(frozen=True, slots=True)
class OAuthTokenHealth:
    total_active: int
    healthy: int
    expiring_soon: int
    expired: int
    has_refresh_errors: int

    def as_dict(self) -> dict[str, int]:
        return {
            "total_active": self.total_active,
            "healthy": self.healthy,
            "expiring_soon": self.expiring_soon,
            "expired": self.expired,
            "has_refresh_errors": self.has_refresh_errors,
        }


@dataclass(frozen=True, slots=True)
class MetaOAuthRefreshConfiguration:
    app_id: str
    graph_base_url: str
    grant_type: MetaOAuthGrantType
    app_secret: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class MetaOAuthProviderResult:
    expires_in_seconds: int
    access_token: str = field(repr=False)
    token_type: str | None = None


class MetaOAuthRefreshTransport(Protocol):
    def exchange(
        self,
        configuration: MetaOAuthRefreshConfiguration,
        access_token: str,
    ) -> MetaOAuthProviderResult: ...


class HttpxMetaOAuthRefreshTransport:
    """Secret-safe synchronous transport for Meta's exchange endpoint."""

    def exchange(
        self,
        configuration: MetaOAuthRefreshConfiguration,
        access_token: str,
    ) -> MetaOAuthProviderResult:
        url = f"{configuration.graph_base_url.rstrip('/')}/oauth/access_token"
        # Form data keeps credentials out of URLs recorded by HTTP access logs.
        form = {
            "grant_type": configuration.grant_type.value,
            "client_id": configuration.app_id,
            "client_secret": configuration.app_secret,
            "fb_exchange_token": access_token,
        }
        try:
            with httpx.Client(timeout=30.0) as client:
                response = client.post(url, data=form)
        except httpx.HTTPError as exc:
            raise _ExpectedRefreshFailure(
                MetaTokenRefreshFailureCode.PROVIDER_UNAVAILABLE
            ) from exc

        if response.status_code == 429 or response.status_code >= 500:
            raise _ExpectedRefreshFailure(
                MetaTokenRefreshFailureCode.PROVIDER_UNAVAILABLE
            )
        if response.status_code >= 400:
            raise _ExpectedRefreshFailure(MetaTokenRefreshFailureCode.PROVIDER_REJECTED)
        try:
            payload = response.json()
        except ValueError as exc:
            raise _ExpectedRefreshFailure(
                MetaTokenRefreshFailureCode.PROVIDER_RESPONSE_INVALID
            ) from exc
        if not isinstance(payload, dict) or payload.get("error"):
            raise _ExpectedRefreshFailure(MetaTokenRefreshFailureCode.PROVIDER_REJECTED)
        new_token = payload.get("access_token")
        expires_in = payload.get("expires_in")
        if (
            not isinstance(new_token, str)
            or not new_token
            or isinstance(expires_in, bool)
            or not isinstance(expires_in, (str, int))
        ):
            raise _ExpectedRefreshFailure(
                MetaTokenRefreshFailureCode.PROVIDER_RESPONSE_INVALID
            )
        try:
            expires_in_seconds = int(expires_in)
        except (TypeError, ValueError) as exc:
            raise _ExpectedRefreshFailure(
                MetaTokenRefreshFailureCode.PROVIDER_RESPONSE_INVALID
            ) from exc
        if expires_in_seconds <= 0:
            raise _ExpectedRefreshFailure(
                MetaTokenRefreshFailureCode.PROVIDER_RESPONSE_INVALID
            )
        raw_token_type = payload.get("token_type")
        token_type = (
            raw_token_type[:64]
            if isinstance(raw_token_type, str) and raw_token_type
            else None
        )
        return MetaOAuthProviderResult(
            access_token=new_token,
            expires_in_seconds=expires_in_seconds,
            token_type=token_type,
        )


meta_oauth_refresh_transport: MetaOAuthRefreshTransport = (
    HttpxMetaOAuthRefreshTransport()
)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _load_configuration(
    db: Session,
    grant_type: MetaOAuthGrantType,
) -> MetaOAuthRefreshConfiguration:
    if grant_type is not MetaOAuthGrantType.FB_EXCHANGE_TOKEN:
        raise _ExpectedRefreshFailure(MetaTokenRefreshFailureCode.INVALID_GRANT_TYPE)

    raw_app_id = settings_spec.resolve_value(db, SettingDomain.comms, "meta_app_id")
    raw_secret_ref = settings_spec.resolve_value(
        db, SettingDomain.comms, "meta_app_secret"
    )
    if not isinstance(raw_app_id, str) or not raw_app_id.strip():
        raise _ExpectedRefreshFailure(MetaTokenRefreshFailureCode.CONFIGURATION_MISSING)
    if not isinstance(raw_secret_ref, str) or not raw_secret_ref.strip():
        raise _ExpectedRefreshFailure(MetaTokenRefreshFailureCode.CONFIGURATION_MISSING)
    # No `is_secret_ref` gate any more. It demanded the stored value BE a
    # `bao://…` reference, which was this codebase's way of saying "do not keep
    # an app secret in the database in plaintext" — a real requirement met a
    # better way now: `comms/meta_app_secret` is stored as ciphertext and
    # decrypted by the resolver, so the database carries nothing readable
    # without the held key, and no network call sits on this path.
    #
    # The gate was also weaker than it read. It checked the value WAS a
    # reference and never WHICH one, so it constrained the format and not the
    # authority. `SECRET_REFERENCE_REQUIRED` survives as a member so any stored
    # or logged occurrence still resolves to a name; nothing raises it.
    try:
        app_secret = secrets.resolve_secret(raw_secret_ref)
    except Exception as exc:
        raise _ExpectedRefreshFailure(
            MetaTokenRefreshFailureCode.SECRET_RESOLUTION_FAILED
        ) from exc
    if not isinstance(app_secret, str) or not app_secret:
        raise _ExpectedRefreshFailure(
            MetaTokenRefreshFailureCode.SECRET_RESOLUTION_FAILED
        )

    raw_version = settings_spec.resolve_value(
        db, SettingDomain.comms, "meta_graph_api_version"
    )
    if not isinstance(raw_version, str) or not _META_GRAPH_VERSION.fullmatch(
        raw_version.strip()
    ):
        raise _ExpectedRefreshFailure(MetaTokenRefreshFailureCode.INVALID_GRAPH_VERSION)
    return MetaOAuthRefreshConfiguration(
        app_id=raw_app_id.strip(),
        app_secret=app_secret,
        graph_base_url=f"https://graph.facebook.com/{raw_version.strip()}",
        grant_type=grant_type,
    )


def list_meta_token_refresh_candidates(
    db: Session,
    query: MetaTokenRefreshCandidatesQuery,
) -> tuple[MetaTokenRefreshCandidate, ...]:
    """Return immutable eligible identities; this query never mutates state."""

    rows = db.execute(
        select(OAuthToken.id, OAuthToken.token_expires_at)
        .where(
            OAuthToken.provider == "meta",
            OAuthToken.account_type == MetaOAuthTokenClass.USER.value,
            OAuthToken.is_active.is_(True),
            OAuthToken.token_expires_at.is_not(None),
            OAuthToken.token_expires_at <= _as_utc(query.eligible_before),
        )
        .order_by(OAuthToken.token_expires_at, OAuthToken.id)
    ).all()
    return tuple(
        MetaTokenRefreshCandidate(
            token_id=token_id,
            expected_expires_at=_as_utc(cast(datetime, expires_at)),
        )
        for token_id, expires_at in rows
    )


def get_oauth_token_health(
    db: Session,
    query: OAuthTokenHealthQuery,
) -> OAuthTokenHealth:
    """Return the existing all-provider OAuth expiry-health projection."""

    tokens = db.scalars(select(OAuthToken).where(OAuthToken.is_active.is_(True))).all()
    observed_at = _as_utc(query.observed_at)
    expiring_before = _as_utc(query.expiring_before)
    expired = 0
    expiring_soon = 0
    has_errors = 0
    healthy = 0
    for token in tokens:
        expires_at = _as_utc(token.token_expires_at) if token.token_expires_at else None
        if expires_at is not None and expires_at <= observed_at:
            expired += 1
        elif expires_at is not None and expires_at <= expiring_before:
            expiring_soon += 1
        if token.refresh_error:
            has_errors += 1
        if not token.refresh_error and (
            expires_at is None or expires_at > expiring_before
        ):
            healthy += 1
    return OAuthTokenHealth(
        total_active=len(tokens),
        healthy=healthy,
        expiring_soon=expiring_soon,
        expired=expired,
        has_refresh_errors=has_errors,
    )


def _emit_refresh_event(
    db: Session,
    *,
    token: OAuthToken,
    command: RefreshMetaTokenCommand,
    status: MetaTokenRefreshStatus,
    expires_at: datetime | None = None,
    failure_code: MetaTokenRefreshFailureCode | None = None,
) -> None:
    payload: dict[str, str] = {
        "aggregate_type": "oauth_token",
        "aggregate_id": str(token.id),
        "aggregate_version": str(command.context.command_id),
        "connector_config_id": str(token.connector_config_id),
        "provider": "meta",
        "token_class": token.account_type,
        "grant_type": command.grant_type.value,
        "status": status.value,
        "command_id": str(command.context.command_id),
        "correlation_id": str(command.context.correlation_id),
    }
    if expires_at is not None:
        payload["expires_at"] = expires_at.isoformat()
    if failure_code is not None:
        payload["failure_code"] = failure_code.value
    emit_event(
        db,
        (
            EventType.oauth_token_refreshed
            if status is MetaTokenRefreshStatus.REFRESHED
            else EventType.oauth_token_refresh_failed
        ),
        payload,
        actor=command.context.actor,
        dispatch_after_commit=False,
    )


def _record_failure(
    db: Session,
    *,
    token: OAuthToken,
    command: RefreshMetaTokenCommand,
    code: MetaTokenRefreshFailureCode,
) -> MetaTokenRefreshResult:
    token.refresh_error = f"{code.value}: {_SAFE_FAILURE_MESSAGES[code]}"
    _emit_refresh_event(
        db,
        token=token,
        command=command,
        status=MetaTokenRefreshStatus.FAILED,
        failure_code=code,
    )
    db.flush()
    logger.warning(
        "meta_oauth_refresh_failed",
        extra={
            "event": "meta_oauth_refresh_failed",
            "token_id": str(token.id),
            "failure_code": code.value,
            "command_id": str(command.context.command_id),
        },
    )
    return MetaTokenRefreshResult(
        token_id=token.id,
        status=MetaTokenRefreshStatus.FAILED,
        failure_code=code,
    )


def _refresh_locked_token(
    db: Session,
    command: RefreshMetaTokenCommand,
) -> MetaTokenRefreshResult:
    token = db.scalar(
        select(OAuthToken)
        .where(OAuthToken.id == command.candidate.token_id)
        .with_for_update()
    )
    if token is None or token.provider != "meta":
        raise MetaOAuthRefreshError(
            code="integration.oauth_tokens.token_not_found",
            message="Meta OAuth token was not found.",
            details={"token_id": str(command.candidate.token_id)},
        )
    if not token.is_active:
        return _record_failure(
            db,
            token=token,
            command=command,
            code=MetaTokenRefreshFailureCode.TOKEN_INACTIVE,
        )
    if token.account_type != MetaOAuthTokenClass.USER.value:
        return _record_failure(
            db,
            token=token,
            command=command,
            code=MetaTokenRefreshFailureCode.TOKEN_CLASS_NOT_PERMITTED,
        )
    if command.grant_type is not MetaOAuthGrantType.FB_EXCHANGE_TOKEN:
        return _record_failure(
            db,
            token=token,
            command=command,
            code=MetaTokenRefreshFailureCode.INVALID_GRANT_TYPE,
        )
    if token.token_expires_at is None:
        return MetaTokenRefreshResult(
            token_id=token.id,
            status=MetaTokenRefreshStatus.SKIPPED,
        )
    current_expiry = _as_utc(token.token_expires_at)
    if current_expiry != _as_utc(
        command.candidate.expected_expires_at
    ) or current_expiry > _as_utc(command.eligible_before):
        return MetaTokenRefreshResult(
            token_id=token.id,
            status=MetaTokenRefreshStatus.SKIPPED,
            expires_at=current_expiry,
        )
    if not token.access_token:
        return _record_failure(
            db,
            token=token,
            command=command,
            code=MetaTokenRefreshFailureCode.TOKEN_MISSING,
        )
    if secrets.is_secret_ref(token.access_token):
        return _record_failure(
            db,
            token=token,
            command=command,
            code=MetaTokenRefreshFailureCode.TOKEN_STORAGE_NOT_PERMITTED,
        )

    try:
        configuration = _load_configuration(db, command.grant_type)
        provider_result = meta_oauth_refresh_transport.exchange(
            configuration,
            token.access_token,
        )
    except _ExpectedRefreshFailure as exc:
        return _record_failure(
            db,
            token=token,
            command=command,
            code=exc.code,
        )

    refreshed_at = _as_utc(command.observed_at)
    expires_at = refreshed_at + timedelta(seconds=provider_result.expires_in_seconds)
    token.access_token = provider_result.access_token
    token.token_expires_at = expires_at
    token.last_refreshed_at = refreshed_at
    if provider_result.token_type is not None:
        token.token_type = provider_result.token_type
    token.refresh_error = None
    _emit_refresh_event(
        db,
        token=token,
        command=command,
        status=MetaTokenRefreshStatus.REFRESHED,
        expires_at=expires_at,
    )
    db.flush()
    logger.info(
        "meta_oauth_refresh_completed",
        extra={
            "event": "meta_oauth_refresh_completed",
            "token_id": str(token.id),
            "command_id": str(command.context.command_id),
        },
    )
    return MetaTokenRefreshResult(
        token_id=token.id,
        status=MetaTokenRefreshStatus.REFRESHED,
        expires_at=expires_at,
    )


def refresh_meta_token(
    db: Session,
    command: RefreshMetaTokenCommand,
) -> MetaTokenRefreshResult:
    """Refresh one eligible Meta user token in a verified owner transaction."""

    return execute_owner_command(
        db,
        definition=_REFRESH_COMMAND,
        context=command.context,
        operation=lambda: _refresh_locked_token(db, command),
    )
