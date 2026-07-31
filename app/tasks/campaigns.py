"""Celery tasks for native campaign delivery."""

from __future__ import annotations

import logging

from app.celery_app import celery_app
from app.services.db_session_adapter import db_session_adapter

logger = logging.getLogger(__name__)


@celery_app.task(name="app.tasks.campaigns.process_due_campaigns")
def process_due_campaigns(*, limit: int = 20) -> dict[str, int]:
    from app.services import comms_campaigns

    with db_session_adapter.session() as session:
        result = comms_campaigns.process_due_campaigns(session, limit=limit)
        session.commit()
        logger.info(
            "campaign processing complete",
            extra={"event": "campaign_processing_complete", "counts": result},
        )
        return result


@celery_app.task(name="app.tasks.campaigns.process_due_campaign_steps")
def process_due_campaign_steps(*, limit: int = 20) -> dict[str, int]:
    """Advance nurture sequences whose next step has come due."""
    from app.services import comms_campaigns

    with db_session_adapter.session() as session:
        result = comms_campaigns.process_due_campaign_steps(session, limit=limit)
        session.commit()
        # The result dict is nested under one key: spreading it into ``extra``
        # collides with reserved LogRecord attributes (its ``created`` count
        # shadowed LogRecord.created and failed every run).
        logger.info(
            "campaign step processing complete",
            extra={"event": "campaign_step_processing_complete", "counts": result},
        )
        return result


@celery_app.task(name="app.tasks.campaigns.send_campaign_batch")
def send_campaign_batch(
    *,
    campaign_id: str,
    batch_size: int = 100,
) -> dict[str, object]:
    from app.services import comms_campaigns

    with db_session_adapter.session() as session:
        result = comms_campaigns.send_campaign_batch(
            session,
            campaign_id,
            batch_size=batch_size,
        )
        session.commit()
        payload = {
            "campaign_id": str(result.campaign_id),
            "queued": result.queued,
            "sent": result.sent,
            "failed": result.failed,
            "skipped": result.skipped,
            "suppressed": result.suppressed,
            "completed": result.completed,
        }
        logger.info(
            "campaign batch send complete",
            extra={"event": "campaign_batch_send_complete", "counts": payload},
        )
        return payload
