"""Scheduled channel/queue health observation (docs/designs/CHANNEL_OBSERVABILITY.md)."""

from __future__ import annotations

import logging

from app.celery_app import celery_app
from app.services.db_session_adapter import db_session_adapter

logger = logging.getLogger(__name__)


@celery_app.task(name="app.tasks.channel_health.observe_channel_health")
def observe_channel_health() -> dict[str, int | str]:
    from app.services import channel_health

    with db_session_adapter.session() as session:
        summary = channel_health.publish_channel_health(session)
        logger.info(
            "channel health observation complete",
            extra={"event": "channel_health_observation", **summary},
        )
        return dict(summary)
