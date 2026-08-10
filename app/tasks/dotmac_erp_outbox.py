"""Celery task: deliver the ``field_erp_sync_events`` outbox to DotMac ERP.

Beat-driven sweep. Gated by ``dotmac_erp_sync_enabled`` (default off) at the
scheduler, so it is inert until a flow is cut over to sub. Delivery itself is
further gated per-flow by ``sync_flow_ownership`` inside ``deliver_pending`` — a
row for a flow sub does not own is skipped, never posted.

Reliability contract: BEAT_RERUN. Each row carries a stable idempotency key
(sent to ERP), transient failures leave the row pending for the next run, and
permanent / budget-exhausted rows dead-letter in the table itself — so a failed
run self-heals and re-delivery is safe.
"""

from __future__ import annotations

import logging
import time

from app.celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(name="app.tasks.dotmac_erp_outbox.refresh_material_catalog")
def refresh_material_catalog() -> dict:
    """Refresh ERP item/warehouse facts without changing Sub eligibility."""
    from app.services.field.material_catalog_sync import run_erp_material_catalog_sync

    return run_erp_material_catalog_sync()


@celery_app.task(name="app.tasks.dotmac_erp_outbox.deliver_erp_sync_events")
def deliver_erp_sync_events() -> dict:
    """Deliver pending field_erp_sync_events rows to ERP."""
    from app.metrics import observe_job

    start = time.monotonic()
    status = "success"
    logger.info("DELIVER_ERP_SYNC_EVENTS_START")
    results: dict[str, object] = {}
    try:
        from app.services.dotmac_erp.outbox import run_deliver_pending

        results = run_deliver_pending()
    except Exception:
        status = "error"
        raise
    finally:
        observe_job("deliver_erp_sync_events", status, time.monotonic() - start)

    logger.info("DELIVER_ERP_SYNC_EVENTS_COMPLETE %s", results)
    return results


@celery_app.task(name="app.tasks.dotmac_erp_outbox.refresh_expense_claim_statuses")
def refresh_expense_claim_statuses() -> dict:
    """Poll ERP for in-flight expense-claim statuses and refresh mirror fields.

    Read-only reconcile: for each synced FieldExpenseRequest still awaiting an ERP
    decision, GET the claim status and write it back. Gated at the scheduler by
    ``dotmac_erp_sync_enabled`` (default off), so it is inert until cutover; a
    no-op when nothing is in flight. Idempotent — safe to re-run.
    """
    from app.metrics import observe_job

    start = time.monotonic()
    status = "success"
    logger.info("REFRESH_EXPENSE_CLAIM_STATUSES_START")
    results: dict[str, object] = {}
    try:
        from app.services.dotmac_erp.expense_sync import (
            run_refresh_expense_claim_statuses,
        )

        results = run_refresh_expense_claim_statuses()
    except Exception:
        status = "error"
        raise
    finally:
        observe_job("refresh_expense_claim_statuses", status, time.monotonic() - start)

    logger.info("REFRESH_EXPENSE_CLAIM_STATUSES_COMPLETE %s", results)
    return results


@celery_app.task(name="app.tasks.dotmac_erp_outbox.refresh_material_request_statuses")
def refresh_material_request_statuses() -> dict:
    """Poll ERP for in-flight material-request statuses and refresh mirror fields.

    Read-only reconcile: for each synced FieldMaterialRequest still awaiting ERP
    fulfillment, GET the request status and write it back (flipping the sub row to
    fulfilled when ERP reports it). Gated at the scheduler by
    ``dotmac_erp_sync_enabled`` (default off), so it is inert until cutover; a
    no-op when nothing is in flight. Idempotent — safe to re-run.
    """
    from app.metrics import observe_job

    start = time.monotonic()
    status = "success"
    logger.info("REFRESH_MATERIAL_REQUEST_STATUSES_START")
    results: dict[str, object] = {}
    try:
        from app.services.dotmac_erp.material_sync import (
            run_refresh_material_request_statuses,
        )

        results = run_refresh_material_request_statuses()
    except Exception:
        status = "error"
        raise
    finally:
        observe_job(
            "refresh_material_request_statuses", status, time.monotonic() - start
        )

    logger.info("REFRESH_MATERIAL_REQUEST_STATUSES_COMPLETE %s", results)
    return results


@celery_app.task(name="app.tasks.dotmac_erp_outbox.repair_purchase_invoice_sync")
def repair_purchase_invoice_sync() -> dict:
    """Queue PO-ready invoices and retry attachment uploads."""
    from app.services.dotmac_erp.purchase_invoice_sync import (
        run_repair_purchase_invoice_sync,
    )

    return run_repair_purchase_invoice_sync()


@celery_app.task(name="app.tasks.dotmac_erp_outbox.refresh_purchase_invoice_statuses")
def refresh_purchase_invoice_statuses() -> dict:
    """Poll ERP for current vendor supplier-invoice settlement observations."""
    from app.metrics import observe_job

    start = time.monotonic()
    status = "success"
    logger.info("REFRESH_PURCHASE_INVOICE_STATUSES_START")
    results: dict[str, object] = {}
    try:
        from app.services.dotmac_erp.purchase_invoice_sync import (
            run_refresh_purchase_invoice_statuses,
        )

        results = run_refresh_purchase_invoice_statuses()
    except Exception:
        status = "error"
        raise
    finally:
        observe_job(
            "refresh_purchase_invoice_statuses", status, time.monotonic() - start
        )

    logger.info("REFRESH_PURCHASE_INVOICE_STATUSES_COMPLETE %s", results)
    return results


@celery_app.task(name="app.tasks.dotmac_erp_outbox.sync_erp_operational_domains")
def sync_erp_operational_domains() -> dict:
    """Push native project, ticket and work-order context to ERP."""
    from app.services.dotmac_erp.domain_sync import run_sync_operational_domains

    return run_sync_operational_domains()
