"""Thin Celery adapters for OAuth token refresh and health reporting."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from typing import Protocol
from uuid import NAMESPACE_URL, uuid4, uuid5

from app.celery_app import celery_app
from app.logging import get_logger
from app.services import meta_oauth
from app.services.db_session_adapter import db_session_adapter
from app.services.domain_errors import DomainError
from app.services.observability import record_task_run
from app.services.owner_commands import CommandContext

logger = get_logger(__name__)


class _TaskRequest(Protocol):
    id: str | None


class _BoundTask(Protocol):
    request: _TaskRequest


def _buffer_days(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("OAuth refresh buffer_days must be an integer")
    if value < 0 or value > 365:
        raise ValueError("OAuth refresh buffer_days must be between 0 and 365")
    return value


@celery_app.task(bind=True, name="app.tasks.oauth.refresh_expiring_tokens")
def refresh_expiring_tokens(
    self: _BoundTask,
    buffer_days: int = 7,
) -> dict[str, int]:
    """Ask the OAuth owner to refresh each eligible token independently."""

    started_at = time.monotonic()
    observed_at = datetime.now(UTC)
    eligible_before = observed_at + timedelta(days=_buffer_days(buffer_days))
    request_id = str(self.request.id or uuid4())
    summary = meta_oauth.MetaTokenRefreshSummary(
        total_checked=0,
        refreshed=0,
        errors=0,
    )
    run_status = "success"
    try:
        with db_session_adapter.read_session() as db:
            candidates = meta_oauth.list_meta_token_refresh_candidates(
                db,
                meta_oauth.MetaTokenRefreshCandidatesQuery(
                    eligible_before=eligible_before
                ),
            )

        refreshed = 0
        errors = 0
        skipped = 0
        logger.info(
            "oauth_token_refresh_started",
            extra={
                "event": "oauth_token_refresh_started",
                "tokens_to_refresh": len(candidates),
                "buffer_days": buffer_days,
            },
        )
        for candidate in candidates:
            command_id = uuid5(
                NAMESPACE_URL,
                (
                    "dotmac:meta-oauth-refresh:"
                    f"{request_id}:{candidate.token_id}:"
                    f"{candidate.expected_expires_at.isoformat()}"
                ),
            )
            command = meta_oauth.RefreshMetaTokenCommand(
                context=CommandContext.system(
                    actor="celery:meta-oauth-refresh",
                    scope=meta_oauth.META_OAUTH_REFRESH_SCOPE,
                    reason="Refresh an expiring Meta OAuth user token",
                    command_id=command_id,
                    idempotency_key=f"celery:{request_id}:{candidate.token_id}",
                ),
                candidate=candidate,
                observed_at=observed_at,
                eligible_before=eligible_before,
            )
            try:
                with db_session_adapter.owner_command_session() as db:
                    result = meta_oauth.refresh_meta_token(db, command)
            except DomainError as exc:
                errors += 1
                logger.warning(
                    "oauth_token_refresh_rejected",
                    extra={
                        "event": "oauth_token_refresh_rejected",
                        "token_id": str(candidate.token_id),
                        "domain_error_code": exc.code,
                        "command_id": str(command_id),
                    },
                )
                continue
            except Exception as exc:
                errors += 1
                logger.error(
                    "oauth_token_refresh_unexpected_error",
                    extra={
                        "event": "oauth_token_refresh_unexpected_error",
                        "token_id": str(candidate.token_id),
                        "error_type": type(exc).__name__,
                        "command_id": str(command_id),
                    },
                )
                continue

            if result.status is meta_oauth.MetaTokenRefreshStatus.REFRESHED:
                refreshed += 1
            elif result.status is meta_oauth.MetaTokenRefreshStatus.FAILED:
                errors += 1
            else:
                skipped += 1

        summary = meta_oauth.MetaTokenRefreshSummary(
            total_checked=len(candidates),
            refreshed=refreshed,
            errors=errors,
        )
        run_status = "degraded" if errors else "success"
        logger.info(
            "oauth_token_refresh_completed",
            extra={
                "event": "oauth_token_refresh_completed",
                "refreshed": refreshed,
                "errors": errors,
                "skipped": skipped,
                "total_checked": len(candidates),
            },
        )
        return summary.as_dict()
    except Exception as exc:
        run_status = "error"
        logger.error(
            "oauth_token_refresh_task_failed",
            extra={
                "event": "oauth_token_refresh_task_failed",
                "error_type": type(exc).__name__,
            },
        )
        raise
    finally:
        record_task_run(
            "oauth_token_refresh",
            status=run_status,
            counters=summary.as_dict(),
            duration_seconds=time.monotonic() - started_at,
        )


@celery_app.task(name="app.tasks.oauth.check_token_health")
def check_token_health() -> dict[str, int]:
    """Serialize the OAuth owner's read-only expiry-health projection."""

    observed_at = datetime.now(UTC)
    with db_session_adapter.read_session() as db:
        result = meta_oauth.get_oauth_token_health(
            db,
            meta_oauth.OAuthTokenHealthQuery(
                observed_at=observed_at,
                expiring_before=observed_at + timedelta(days=7),
            ),
        )
    payload = result.as_dict()
    logger.info(
        "oauth_token_health_check",
        extra={"event": "oauth_token_health_check", **payload},
    )
    return payload
