"""Compatibility tombstones for already queued retired CRM quote jobs."""

import logging

from app.celery_app import celery_app
from app.services.quote_retirement import retirement_outcome

logger = logging.getLogger(__name__)


def _retired() -> dict[str, object]:
    outcome = retirement_outcome()
    logger.info("crm_quote_reconciliation_retired")
    return outcome.model_dump(mode="json")


@celery_app.task(name="app.tasks.quotes.reconcile_quote_mirror")
def reconcile_quote_mirror() -> dict[str, object]:
    return _retired()


@celery_app.task(name="app.tasks.quotes.refresh_quote_mirror_for_subscriber")
def refresh_quote_mirror_for_subscriber(subscriber_id: str) -> dict[str, object]:
    return _retired()
