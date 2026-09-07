"""Scheduled collection of LLDP adjacency observations.

Reads each MikroTik NAS's /ip/neighbor and reconciles observation rows. Routed
to the ``ingestion`` queue. Read-only against routers; commits the observation
upsert on success and makes no forwarding decision.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime

from billiard.exceptions import SoftTimeLimitExceeded
from sqlalchemy.orm import Session

from app.celery_app import celery_app
from app.services.db_session_adapter import db_session_adapter
from app.services.owner_commands import CommandContext
from app.services.topology.coverage_metrics import store_task_stats
from app.services.topology.lldp_contracts import LldpReadQuery, ReconcileLldpCommand

logger = logging.getLogger(__name__)


@contextmanager
def _poll_session() -> Iterator[Session]:
    """Close read/owner sessions even when rollback or cleanup itself fails."""
    db = db_session_adapter.create_session()
    failed = False
    try:
        yield db
    except BaseException:
        failed = True
        raise
    finally:
        try:
            try:
                db_session_adapter.discard_failed_transaction(db)
            finally:
                db.close()
        except Exception:
            if not failed:
                raise
            logger.warning("lldp_topology_poll_cleanup_failed")


def _record_stats(stats: dict[str, int] | dict[str, str]) -> None:
    try:
        store_task_stats("lldp_poll", stats)
    except Exception:
        logger.warning("lldp_topology_poll_stats_failed")


@celery_app.task(
    name="app.tasks.topology_lldp.run_lldp_topology_poll",
    soft_time_limit=300,
    time_limit=360,
)
def run_lldp_topology_poll() -> dict[str, int]:
    """Poll the fleet's LLDP neighbors and reconcile adjacency observations."""
    from app.services.topology.lldp_poller import (
        poll_all,
        read_snapshot,
        reconcile_poll,
    )

    try:
        with _poll_session() as db:
            snapshot = read_snapshot(db, query=LldpReadQuery(datetime.now(UTC)))
        poll = poll_all(snapshot=snapshot)
        command = ReconcileLldpCommand(
            context=CommandContext.system(
                actor="task:lldp_poll",
                scope="network:topology",
                reason="scheduled LLDP observation reconciliation",
            ),
            poll=poll,
        )
        with _poll_session() as db:
            result = reconcile_poll(db, command=command).to_dict()
    except SoftTimeLimitExceeded:
        logger.warning("lldp_topology_poll_timed_out")
        _record_stats({"error": "lldp_topology_poll_timed_out"})
        raise
    except Exception as exc:  # noqa: BLE001 - report and roll back
        logger.exception("lldp_topology_poll_failed")
        _record_stats({"error": f"lldp_topology_poll_failed: {type(exc).__name__}"})
        raise
    # Stash the run outcome (success or error) for the topology metrics
    # exporter's pipeline-health gauges.
    _record_stats(result)
    return result
