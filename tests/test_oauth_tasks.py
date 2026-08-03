"""The OAuth Celery tasks remain thin adapters around typed service owners."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from app.services import meta_oauth
from app.services.db_session_adapter import db_session_adapter
from app.tasks import oauth as oauth_tasks


def test_refresh_task_imports_existing_meta_owner() -> None:
    assert oauth_tasks.meta_oauth is meta_oauth


def test_refresh_task_delegates_candidates_and_commands(monkeypatch) -> None:
    fake_read_db = object()
    fake_command_db = object()
    expires_at = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)
    candidates = (
        meta_oauth.MetaTokenRefreshCandidate(uuid4(), expires_at),
        meta_oauth.MetaTokenRefreshCandidate(uuid4(), expires_at),
    )
    seen: dict[str, object] = {"commands": [], "recorded": []}

    @contextmanager
    def read_session():
        seen["read_entered"] = True
        yield fake_read_db
        seen["read_exited"] = True

    @contextmanager
    def owner_command_session():
        seen["owner_sessions"] = int(seen.get("owner_sessions", 0)) + 1
        yield fake_command_db

    def list_candidates(db, query):
        seen["candidate_db"] = db
        seen["candidate_query"] = query
        return candidates

    def refresh(db, command):
        seen["commands"].append((db, command))
        status = (
            meta_oauth.MetaTokenRefreshStatus.REFRESHED
            if command.candidate is candidates[0]
            else meta_oauth.MetaTokenRefreshStatus.FAILED
        )
        return meta_oauth.MetaTokenRefreshResult(
            token_id=command.candidate.token_id,
            status=status,
            failure_code=(
                None
                if status is meta_oauth.MetaTokenRefreshStatus.REFRESHED
                else meta_oauth.MetaTokenRefreshFailureCode.PROVIDER_REJECTED
            ),
        )

    monkeypatch.setattr(db_session_adapter, "read_session", read_session)
    monkeypatch.setattr(
        db_session_adapter, "owner_command_session", owner_command_session
    )
    monkeypatch.setattr(
        meta_oauth, "list_meta_token_refresh_candidates", list_candidates
    )
    monkeypatch.setattr(meta_oauth, "refresh_meta_token", refresh)
    monkeypatch.setattr(
        oauth_tasks,
        "record_task_run",
        lambda name, **kwargs: seen["recorded"].append((name, kwargs)),
    )

    oauth_tasks.refresh_expiring_tokens.push_request(id="oauth-task-test")
    try:
        result = oauth_tasks.refresh_expiring_tokens.run(buffer_days=7)
    finally:
        oauth_tasks.refresh_expiring_tokens.pop_request()

    assert result == {"refreshed": 1, "errors": 1, "total_checked": 2}
    assert seen["read_entered"] is True
    assert seen["read_exited"] is True
    assert seen["candidate_db"] is fake_read_db
    assert isinstance(
        seen["candidate_query"], meta_oauth.MetaTokenRefreshCandidatesQuery
    )
    assert seen["owner_sessions"] == 2
    commands = seen["commands"]
    assert len(commands) == 2
    assert all(db is fake_command_db for db, _command in commands)
    assert all(
        isinstance(command, meta_oauth.RefreshMetaTokenCommand)
        for _db, command in commands
    )
    assert all(
        command.context.actor == "celery:meta-oauth-refresh"
        for _db, command in commands
    )
    assert all(
        command.context.scope == meta_oauth.META_OAUTH_REFRESH_SCOPE
        for _db, command in commands
    )
    assert seen["recorded"][0][0] == "oauth_token_refresh"
    assert seen["recorded"][0][1]["status"] == "degraded"


def test_health_task_delegates_read_projection(monkeypatch) -> None:
    fake_db = object()
    seen: dict[str, object] = {}

    @contextmanager
    def read_session():
        yield fake_db

    def health(db, query):
        seen["db"] = db
        seen["query"] = query
        return meta_oauth.OAuthTokenHealth(
            total_active=4,
            healthy=1,
            expiring_soon=1,
            expired=1,
            has_refresh_errors=1,
        )

    monkeypatch.setattr(db_session_adapter, "read_session", read_session)
    monkeypatch.setattr(meta_oauth, "get_oauth_token_health", health)

    result = oauth_tasks.check_token_health.run()

    assert seen["db"] is fake_db
    assert isinstance(seen["query"], meta_oauth.OAuthTokenHealthQuery)
    assert result == {
        "total_active": 4,
        "healthy": 1,
        "expiring_soon": 1,
        "expired": 1,
        "has_refresh_errors": 1,
    }


def test_oauth_task_source_contains_no_orm_or_transaction_writes() -> None:
    source = (
        Path(__file__).resolve().parents[1] / "app" / "tasks" / "oauth.py"
    ).read_text(encoding="utf-8")

    assert "from app.models" not in source
    assert "from sqlalchemy" not in source
    assert ".query(" not in source
    assert ".commit(" not in source
    assert ".rollback(" not in source
    assert "db_session_adapter.read_session()" in source
    assert "db_session_adapter.owner_command_session()" in source
    assert "RefreshMetaTokenCommand(" in source


def test_refresh_summary_preserves_existing_task_payload_contract() -> None:
    summary = meta_oauth.MetaTokenRefreshSummary(
        total_checked=3,
        refreshed=2,
        errors=1,
    )
    assert summary.as_dict() == {
        "refreshed": 2,
        "errors": 1,
        "total_checked": 3,
    }


def test_candidate_window_is_typed_datetime() -> None:
    observed_at = datetime.now(UTC)
    query = meta_oauth.MetaTokenRefreshCandidatesQuery(
        eligible_before=observed_at + timedelta(days=7)
    )
    assert query.eligible_before.tzinfo is UTC
