"""Canonical, concurrency-safe refresh-token rotation owner."""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models.auth import Session as AuthSession
from app.models.auth import SessionStatus
from app.services import auth_token_signing, staff_party_authentication
from app.services.domain_errors import DomainError
from app.services.events import emit_event
from app.services.events.types import EventType
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)

OWNER = "app_sessions.refresh"
CONCERN = "concurrency-safe database authentication session renewal"
REFRESH_REPLAY_OVERLAP = timedelta(seconds=5)

_REFRESH_COMMAND = OwnerCommandDefinition(
    owner=OWNER,
    concern=CONCERN,
    name="renew_authentication_session",
)


class RefreshDisposition(StrEnum):
    ROTATED = "rotated"
    DUPLICATE = "duplicate"
    EXPIRED = "expired"
    REUSE_REVOKED = "reuse_revoked"


class SessionPrincipalType(StrEnum):
    SUBSCRIBER = "subscriber"
    SYSTEM_USER = "system_user"
    RESELLER_USER = "reseller_user"


class RefreshSessionError(DomainError):
    """Stable, transport-neutral refresh refusal."""


@dataclass(frozen=True, slots=True)
class RefreshSessionCommand:
    context: CommandContext
    refresh_token: str
    client_ip: str
    user_agent: str | None
    device_id: str | None = None
    observed_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class RefreshSessionOutcome:
    session_id: UUID
    principal_id: UUID
    principal_type: SessionPrincipalType
    disposition: RefreshDisposition
    access_token: str | None
    refresh_token: str | None
    decided_at: datetime

    @property
    def accepted(self) -> bool:
        return self.disposition in {
            RefreshDisposition.ROTATED,
            RefreshDisposition.DUPLICATE,
        }


def hash_refresh_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def normalize_user_agent(value: str | None, max_len: int = 512) -> str | None:
    if not value:
        return value
    return value[:max_len]


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def _principal(session: AuthSession) -> tuple[SessionPrincipalType, UUID]:
    if session.system_user_id:
        return SessionPrincipalType.SYSTEM_USER, session.system_user_id
    if session.reseller_user_id:
        return SessionPrincipalType.RESELLER_USER, session.reseller_user_id
    if session.subscriber_id:
        return SessionPrincipalType.SUBSCRIBER, session.subscriber_id
    raise RefreshSessionError(
        code=f"{OWNER}.invalid_principal",
        message="The authentication session has no principal.",
        details={"session_id": str(session.id)},
    )


def _same_client(session: AuthSession, command: RefreshSessionCommand) -> bool:
    if session.device_id or command.device_id:
        return bool(
            session.device_id
            and command.device_id
            and secrets.compare_digest(session.device_id, command.device_id)
        )
    return (
        session.ip_address == command.client_ip
        and session.user_agent == normalize_user_agent(command.user_agent)
    )


def _event_payload(
    command: RefreshSessionCommand,
    session: AuthSession,
    disposition: RefreshDisposition,
) -> dict[str, str]:
    return {
        "aggregate_type": "authentication_session",
        "aggregate_id": str(session.id),
        "aggregate_version": str(command.context.command_id),
        "session_id": str(session.id),
        "disposition": disposition.value,
        "command_id": str(command.context.command_id),
        "correlation_id": str(command.context.correlation_id),
        "scope": command.context.scope,
        "reason": command.context.reason,
    }


def _issue_access_token(
    db: Session,
    *,
    session: AuthSession,
    principal_id: UUID,
    principal_type: SessionPrincipalType,
    issued_at: datetime,
) -> str:
    try:
        return auth_token_signing.issue_access_token(
            db,
            principal_id=str(principal_id),
            principal_type=principal_type.value,
            session_id=str(session.id),
            issued_at=issued_at,
        )
    except auth_token_signing.TokenSigningConfigurationError as exc:
        raise RefreshSessionError(
            code=f"{OWNER}.signing_unavailable",
            message="Authentication token signing is unavailable.",
        ) from exc


def renew_authentication_session(
    db: Session,
    command: RefreshSessionCommand,
) -> RefreshSessionOutcome:
    """Serialize rotation and safely replay one just-rotated browser token."""

    def operation() -> RefreshSessionOutcome:
        now = _as_utc(command.observed_at) or datetime.now(UTC)
        supplied_hash = hash_refresh_token(command.refresh_token)
        statement = (
            select(AuthSession)
            .where(
                or_(
                    AuthSession.token_hash == supplied_hash,
                    AuthSession.previous_token_hash == supplied_hash,
                ),
                AuthSession.status == SessionStatus.active,
                AuthSession.revoked_at.is_(None),
            )
            .with_for_update()
        )
        session = db.scalars(statement).first()
        if session is None:
            raise RefreshSessionError(
                code=f"{OWNER}.invalid_token",
                message="Invalid refresh token.",
            )

        principal_type, principal_id = _principal(session)
        expires_at = _as_utc(session.expires_at)
        if expires_at is not None and expires_at <= now:
            session.status = SessionStatus.expired
            disposition = RefreshDisposition.EXPIRED
            emit_event(
                db,
                EventType.authentication_session_refresh_refused,
                _event_payload(command, session, disposition),
                actor=command.context.actor,
                subscriber_id=(
                    principal_id
                    if principal_type is SessionPrincipalType.SUBSCRIBER
                    else None
                ),
                dispatch_after_commit=False,
            )
            return RefreshSessionOutcome(
                session_id=session.id,
                principal_id=principal_id,
                principal_type=principal_type,
                disposition=disposition,
                access_token=None,
                refresh_token=None,
                decided_at=now,
            )

        is_current = secrets.compare_digest(session.token_hash, supplied_hash)
        if not is_current:
            rotated_at = _as_utc(session.token_rotated_at)
            within_overlap = bool(
                rotated_at
                and now >= rotated_at
                and now - rotated_at <= REFRESH_REPLAY_OVERLAP
            )
            if within_overlap and _same_client(session, command):
                return RefreshSessionOutcome(
                    session_id=session.id,
                    principal_id=principal_id,
                    principal_type=principal_type,
                    disposition=RefreshDisposition.DUPLICATE,
                    access_token=_issue_access_token(
                        db,
                        session=session,
                        principal_id=principal_id,
                        principal_type=principal_type,
                        issued_at=now,
                    ),
                    refresh_token=None,
                    decided_at=now,
                )

            session.status = SessionStatus.revoked
            session.revoked_at = now
            disposition = RefreshDisposition.REUSE_REVOKED
            emit_event(
                db,
                EventType.authentication_session_refresh_refused,
                _event_payload(command, session, disposition),
                actor=command.context.actor,
                subscriber_id=(
                    principal_id
                    if principal_type is SessionPrincipalType.SUBSCRIBER
                    else None
                ),
                dispatch_after_commit=False,
            )
            return RefreshSessionOutcome(
                session_id=session.id,
                principal_id=principal_id,
                principal_type=principal_type,
                disposition=disposition,
                access_token=None,
                refresh_token=None,
                decided_at=now,
            )

        if principal_type is SessionPrincipalType.SYSTEM_USER:
            try:
                principal = staff_party_authentication.resolve_staff_session_principal(
                    db,
                    party_id=session.party_id,
                    system_user_id=session.system_user_id,
                    reference=str(session.id),
                )
            except staff_party_authentication.StaffProjectionError as exc:
                raise RefreshSessionError(
                    code=f"{OWNER}.staff_projection_refused",
                    message="Invalid refresh token.",
                    details={"refusal": exc.refusal.value},
                ) from exc
            principal_id = principal.id

        new_refresh = secrets.token_urlsafe(48)
        session.previous_token_hash = session.token_hash
        session.token_hash = hash_refresh_token(new_refresh)
        session.token_rotated_at = now
        session.last_seen_at = now
        session.ip_address = command.client_ip
        session.user_agent = normalize_user_agent(command.user_agent)
        if command.device_id:
            session.device_id = command.device_id
        emit_event(
            db,
            EventType.authentication_session_rotated,
            _event_payload(command, session, RefreshDisposition.ROTATED),
            actor=command.context.actor,
            subscriber_id=(
                principal_id
                if principal_type is SessionPrincipalType.SUBSCRIBER
                else None
            ),
            dispatch_after_commit=False,
        )
        return RefreshSessionOutcome(
            session_id=session.id,
            principal_id=principal_id,
            principal_type=principal_type,
            disposition=RefreshDisposition.ROTATED,
            access_token=_issue_access_token(
                db,
                session=session,
                principal_id=principal_id,
                principal_type=principal_type,
                issued_at=now,
            ),
            refresh_token=new_refresh,
            decided_at=now,
        )

    return execute_owner_command(
        db,
        definition=_REFRESH_COMMAND,
        context=command.context,
        operation=operation,
    )
