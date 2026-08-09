"""Scheduled UISP topology sync -> cpe_devices/olt_devices/ont_units edges.

Pulls the UISP inventory (read-only) and reconciles the wireless/UFiber
customer-device relationship layer into sub's own tables. Routed to the
``ingestion`` queue like the other topology tasks.

The pass commits at phase boundaries rather than once at the end: it fans out
one UISP request per AP and one per UF-OLT, and a transaction may not be held
across that. A failed pass can therefore leave earlier phases committed, which
is safe because the sync is idempotent — see ``uisp_sync.sync``.

Single-flight: the run is guarded by ``db_session_adapter.advisory_lock``
(the repo's safe helper — rolls back before unlocking and wraps the unlock in
try/except, so the lock can never leak on an aborted transaction). An
overlapping scheduled/on-demand run is skipped, mirroring app/tasks/events.py.
"""

from __future__ import annotations

import logging
from typing import Any

from billiard.exceptions import SoftTimeLimitExceeded

from app.celery_app import celery_app
from app.services.db_session_adapter import db_session_adapter
from app.services.topology.coverage_metrics import store_task_stats
from app.services.uisp import UispClient, UispClientError, uisp_configured

logger = logging.getLogger(__name__)

# Statement timeout for acquiring the advisory lock. Applied with SET LOCAL,
# so it also bounds the sync's statements up to the first phase commit and no
# further; the task's own soft/hard time limits bound the rest of the run. It
# is deliberately not made session-level: this connection returns to the pool,
# and a lingering statement_timeout would leak to whoever checks it out next.
_LOCK_TIMEOUT_MS = 30_000


@celery_app.task(
    name="app.tasks.topology_uisp.run_uisp_topology_sync",
    soft_time_limit=540,
    time_limit=600,
)
def run_uisp_topology_sync() -> dict[str, Any]:
    """Sync UISP customer-device topology into sub's tables."""
    if not uisp_configured():
        return {"skipped": "uisp_token_missing"}

    from app.services.topology.uisp_sync import ADVISORY_LOCK_KEY, sync

    with db_session_adapter.advisory_lock(
        ADVISORY_LOCK_KEY, timeout_ms=_LOCK_TIMEOUT_MS
    ) as (db, acquired):
        if not acquired:
            logger.info("uisp_topology_sync_skipped: previous run still in progress")
            return {"skipped": "already_running"}
        try:
            client = UispClient.from_env()
            result = sync(db, client)
            db.commit()
        except UispClientError as exc:
            db.rollback()
            logger.warning("uisp_topology_sync_failed: %s", exc)
            result = {"error": "uisp_unavailable", "message": str(exc)}
        except SoftTimeLimitExceeded:
            db.rollback()
            logger.warning("uisp_topology_sync_timed_out")
            result = {"error": "uisp_topology_sync_timed_out"}
        except Exception as exc:  # noqa: BLE001 - report and roll back
            db.rollback()
            logger.exception("uisp_topology_sync_failed")
            result = {"error": str(exc)}
        # Stash the run outcome (success or error) for the topology metrics
        # exporter; lock-skips above never reach here, so they can't clobber
        # the last real result.
        store_task_stats("uisp_sync", result)
        return result
