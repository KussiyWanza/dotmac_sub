"""Worker adapter for the verified inbound integration-receipt lease.

Recovers `processing` receipts a claimant died on without calling
`mark_processed`/`mark_failed` — see `app.services.integrations.inbox` for
the owning lifecycle logic (this task is a thin wrapper, modeled on
`app.tasks.events.mark_stale_processing_events`).
"""

from __future__ import annotations

import logging
from datetime import timedelta

from app.celery_app import celery_app
from app.services.db_session_adapter import db_session_adapter
from app.services.integrations import inbox as integration_inbox

logger = logging.getLogger(__name__)

# Advisory lock key for preventing concurrent sweeper runs.
_INBOX_RECLAIM_LOCK_KEY = 70420901


@celery_app.task(name="app.tasks.integration_inbox.reclaim_stale_claims")
def reclaim_stale_claims(grace_minutes: int = 1) -> dict[str, int]:
    """Move `processing` receipts whose lease expired more than grace ago
    to `retryable`. Uses an advisory lock to prevent concurrent runs."""

    with db_session_adapter.advisory_lock(_INBOX_RECLAIM_LOCK_KEY) as (
        session,
        lock_acquired,
    ):
        if not lock_acquired:
            logger.debug(
                "Skipping integration inbox reclaim: previous run still in progress"
            )
            return {"skipped_due_to_lock": 1}

        reclaimed = integration_inbox.execute_command(
            session,
            lambda: integration_inbox.reclaim_stale_claims(
                session, grace=timedelta(minutes=grace_minutes)
            ),
        )
        if reclaimed:
            logger.info(
                "Reclaimed %s stale integration inbox processing claims", reclaimed
            )
        return {"reclaimed": reclaimed}
