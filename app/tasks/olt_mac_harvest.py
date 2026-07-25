"""Bounded Huawei OLT MAC-forwarding harvest tasks.

The scheduled dispatcher is intentionally short-lived. It queues one
independently locked, retryable ingestion task per active Huawei OLT, so one
slow or unreachable device cannot occupy a worker for the whole fleet.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from typing import Any

from sqlalchemy import select

from app.celery_app import celery_app, enqueue_celery_task
from app.services.db_session_adapter import db_session_adapter
from app.tasks._postgres_lock import postgres_session_advisory_lock

logger = logging.getLogger(__name__)


def _olt_harvest_lock_key(olt_id: str) -> int:
    digest = hashlib.blake2b(
        f"olt-mac-harvest:{olt_id}".encode(),
        digest_size=8,
    ).digest()
    return int.from_bytes(digest, "big", signed=True)


@celery_app.task(
    name="app.tasks.olt_mac_harvest.run_single_olt_mac_harvest",
    autoretry_for=(RuntimeError, OSError, TimeoutError),
    retry_backoff=30,
    retry_backoff_max=300,
    retry_jitter=True,
    retry_kwargs={"max_retries": 3},
    soft_time_limit=240,
    time_limit=300,
)
def run_single_olt_mac_harvest(olt_id: str) -> dict[str, Any]:
    """Harvest one OLT with an independent lock, timeout, and retry budget."""

    try:
        parsed_olt_id = uuid.UUID(olt_id)
    except (TypeError, ValueError):
        logger.warning("olt_mac_harvest_invalid_olt_id olt_id=%r", olt_id)
        return {"olt_id": olt_id, "skipped": "invalid_id"}

    with postgres_session_advisory_lock(_olt_harvest_lock_key(olt_id)) as acquired:
        if not acquired:
            return {"olt_id": olt_id, "skipped": "already_running"}

        from app.services.topology.olt_mac_harvest import harvest_olt_mac_table

        with db_session_adapter.session() as db:
            result = harvest_olt_mac_table(db, parsed_olt_id)
            if result["olts_polled"] == 0:
                return {"olt_id": olt_id, "skipped": "not_pollable"}
            if result["olt_errors"]:
                raise RuntimeError(
                    f"OLT MAC harvest failed for {olt_id}; retrying independently"
                )
            return {"olt_id": olt_id, **result}


@celery_app.task(
    name="app.tasks.olt_mac_harvest.run_olt_mac_harvest",
    soft_time_limit=60,
    time_limit=90,
)
def run_olt_mac_harvest() -> dict[str, int]:
    """Queue one independently bounded MAC harvest per active Huawei OLT."""

    from app.models.network import OLTDevice

    with db_session_adapter.read_session() as db:
        olt_ids = list(
            db.scalars(
                select(OLTDevice.id)
                .where(
                    OLTDevice.vendor.ilike("%huawei%"),
                    OLTDevice.is_active.is_(True),
                )
                .order_by(OLTDevice.id)
            ).all()
        )

    queued = 0
    failed = 0
    for olt_id in olt_ids:
        try:
            enqueue_celery_task(
                run_single_olt_mac_harvest,
                args=[str(olt_id)],
                source="network.olt_mac_harvest.scheduled",
            )
            queued += 1
        except Exception:  # noqa: BLE001 - isolate one enqueue from the fleet
            failed += 1
            logger.exception("olt_mac_harvest_dispatch_failed olt_device_id=%s", olt_id)

    return {"olts": len(olt_ids), "queued": queued, "failed": failed}
