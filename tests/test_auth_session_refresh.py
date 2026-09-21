from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.models.auth import Session as AuthSession
from app.models.auth import SessionStatus
from app.services.auth_session_refresh import (
    REFRESH_REPLAY_OVERLAP,
    RefreshDisposition,
    RefreshSessionCommand,
    hash_refresh_token,
    renew_authentication_session,
)
from app.services.owner_commands import CommandContext


def _context(token: str) -> CommandContext:
    return CommandContext.system(
        actor="pytest:auth-refresh",
        scope="authentication:session",
        reason="Verify refresh concurrency policy",
        idempotency_key=f"refresh:{hash_refresh_token(token)}",
    )


def _command(token: str, observed_at: datetime, *, user_agent: str = "browser/1"):
    return RefreshSessionCommand(
        context=_context(token),
        refresh_token=token,
        client_ip="203.0.113.8",
        user_agent=user_agent,
        observed_at=observed_at,
    )


def _session(db_session, person, token: str, now: datetime) -> AuthSession:
    session = AuthSession(
        subscriber_id=person.id,
        status=SessionStatus.active,
        token_hash=hash_refresh_token(token),
        ip_address="203.0.113.8",
        user_agent="browser/1",
        expires_at=now + timedelta(days=1),
    )
    db_session.add(session)
    db_session.commit()
    return session


def test_same_browser_duplicate_gets_access_path_without_second_rotation(
    db_session, person
) -> None:
    now = datetime.now(UTC)
    old_token = "old-refresh-token"
    session = _session(db_session, person, old_token, now)

    first = renew_authentication_session(
        db=db_session, command=_command(old_token, now)
    )
    second = renew_authentication_session(
        db=db_session, command=_command(old_token, now + timedelta(seconds=2))
    )

    assert first.disposition is RefreshDisposition.ROTATED
    assert first.refresh_token is not None
    assert second.disposition is RefreshDisposition.DUPLICATE
    assert second.refresh_token is None
    db_session.refresh(session)
    assert session.token_hash == hash_refresh_token(first.refresh_token)
    assert session.status is SessionStatus.active


def test_previous_token_outside_overlap_revokes_session(db_session, person) -> None:
    now = datetime.now(UTC)
    old_token = "late-old-refresh-token"
    session = _session(db_session, person, old_token, now)

    renew_authentication_session(db=db_session, command=_command(old_token, now))
    refused = renew_authentication_session(
        db=db_session,
        command=_command(
            old_token, now + REFRESH_REPLAY_OVERLAP + timedelta(milliseconds=1)
        ),
    )

    assert refused.disposition is RefreshDisposition.REUSE_REVOKED
    db_session.refresh(session)
    assert session.status is SessionStatus.revoked
    assert session.revoked_at is not None


def test_previous_token_from_different_browser_revokes_inside_overlap(
    db_session, person
) -> None:
    now = datetime.now(UTC)
    old_token = "other-browser-refresh-token"
    session = _session(db_session, person, old_token, now)

    renew_authentication_session(db=db_session, command=_command(old_token, now))
    refused = renew_authentication_session(
        db=db_session,
        command=_command(old_token, now + timedelta(seconds=1), user_agent="browser/2"),
    )

    assert refused.disposition is RefreshDisposition.REUSE_REVOKED
    db_session.refresh(session)
    assert session.status is SessionStatus.revoked
