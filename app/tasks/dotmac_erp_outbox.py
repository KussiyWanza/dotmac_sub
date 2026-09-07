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
from uuid import NAMESPACE_URL, uuid4, uuid5

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
def sync_erp_operational_domains() -> dict[str, object]:
    """Push native project, ticket, project-task, and work-order context to ERP."""
    from app.services import job_heartbeat
    from app.services.dotmac_erp.domain_sync import run_sync_operational_domains

    outcome = run_sync_operational_domains()
    job_heartbeat.record_result(
        "app.tasks.dotmac_erp_outbox.sync_erp_operational_domains",
        status=outcome.status,
        detail=outcome.model_dump(mode="json"),
        next_attempt_at=outcome.next_attempt_at,
    )
    return outcome.model_dump(mode="json")


@celery_app.task(
    name="app.tasks.dotmac_erp_outbox.reconcile_erp_staff_access",
    bind=True,
    max_retries=5,
    default_retry_delay=120,
    retry_backoff=True,
    retry_jitter=True,
)
def reconcile_erp_staff_access(self) -> dict[str, object]:
    """Repair Selfcare staff-access projections from ERP's authoritative feed."""

    from app.schemas.erp_staff_access_webhook import (
        ErpStaffAccountStatusProjection,
        ErpStaffLeaveRestrictionProjection,
    )
    from app.services import erp_staff_access
    from app.services.db_session_adapter import db_session_adapter
    from app.services.dotmac_erp.client import DotMacERPTransientError
    from app.services.integrations.backoffice_contracts import (
        ERP_STAFF_ACCESS_RECONCILE_CAPABILITY,
    )
    from app.services.integrations.erp_capability import ErpCapabilityClient
    from app.services.owner_commands import CommandContext

    page_limit = 500
    try:
        with db_session_adapter.session() as db:
            client = ErpCapabilityClient(db)
            leave_page = client.get_staff_access_projection(
                entity="leave_restriction",
                limit=page_limit,
            )
            account_page = client.get_staff_access_projection(
                entity="account_status",
                limit=page_limit,
            )
            if (
                len(leave_page.items) >= page_limit
                or len(account_page.items) >= page_limit
            ):
                raise RuntimeError(
                    "ERP staff access projection reached the bounded page limit"
                )

            leave_events = tuple(
                leave_event
                for item in leave_page.items
                if isinstance(item, ErpStaffLeaveRestrictionProjection)
                if (leave_event := item.to_owner_event()) is not None
            )
            account_events = tuple(
                account_event
                for item in account_page.items
                if isinstance(item, ErpStaffAccountStatusProjection)
                if (account_event := item.to_owner_event()) is not None
            )
            unmapped = (
                len(leave_page.items)
                + len(account_page.items)
                - len(leave_events)
                - len(account_events)
            )
            task_run_id = self.request.id or str(uuid4())
            command_id = uuid5(
                NAMESPACE_URL,
                f"erp-staff-access-reconcile:{task_run_id}",
            )
            db_session_adapter.release_read_transaction(db)
            outcome = erp_staff_access.reconcile_staff_access_snapshot(
                db,
                erp_staff_access.ReconcileStaffAccessSnapshotCommand(
                    context=CommandContext(
                        command_id=command_id,
                        correlation_id=command_id,
                        actor="service:dotmac-erp-reconcile",
                        scope=ERP_STAFF_ACCESS_RECONCILE_CAPABILITY,
                        reason="Repair ERP staff access projection drift",
                        idempotency_key=task_run_id,
                    ),
                    leave_restrictions=leave_events,
                    account_statuses=account_events,
                ),
            )
    except DotMacERPTransientError as exc:
        raise self.retry(exc=exc) from exc

    result: dict[str, object] = {
        "leave_restrictions_seen": outcome.leave_restrictions_seen,
        "account_statuses_seen": outcome.account_statuses_seen,
        "unmapped_seen": unmapped,
        "applied": outcome.applied,
        "ignored": outcome.ignored,
    }
    logger.info("ERP_STAFF_ACCESS_RECONCILE_COMPLETE %s", result)
    return result
