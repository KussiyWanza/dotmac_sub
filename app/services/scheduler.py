import logging

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.scheduler import ScheduledTask, ScheduleType
from app.schemas.scheduler import ScheduledTaskCreate, ScheduledTaskUpdate
from app.services.common import (
    apply_ordering,
    apply_pagination,
)
from app.services.response import ListResponseMixin

logger = logging.getLogger(__name__)

PERMANENT_LIFECYCLE_TASKS = frozenset(
    {
        "app.tasks.billing.run_invoice_cycle",
        "app.tasks.billing.mark_invoices_overdue",
        "app.tasks.billing.run_billing_notifications",
        "app.tasks.billing.check_billing_switch",
        "app.tasks.collections.run_billing_enforcement",
        "app.tasks.collections.run_bundle_reconcile",
        "app.tasks.collections.prepaid_balance_sweep",
        "app.tasks.autopay.charge_due_invoices",
        "app.tasks.arrangements.check_overdue_arrangements",
        "app.tasks.payment_reconciliation.reconcile_topups",
        "app.tasks.catalog.expire_subscriptions",
        "app.tasks.catalog.apply_due_subscription_changes",
        "app.tasks.catalog.apply_due_subscription_status_commands",
        "app.tasks.vacation_holds.resume_expired_holds",
        "app.tasks.enforcement.reconcile_billing_approval_drift",
        "app.tasks.notifications.deliver_notification_queue",
        "app.tasks.events.dispatch_pending_events",
        "app.tasks.events.retry_failed_events",
        "app.tasks.events.mark_stale_processing_events",
        "app.tasks.radius.run_enforcement_reconciler",
        "app.tasks.radius.reconcile_active_sessions",
        "app.tasks.radius_population.sync_device_login",
        "app.tasks.usage.lift_expired_fup_enforcement",
        "app.tasks.provisioning.retry_pending_compensation_failures",
        "app.tasks.campaigns.process_due_campaigns",
        "app.tasks.campaigns.process_due_campaign_steps",
        "app.tasks.device_projection.reconcile_device_projections",
        "app.tasks.monitoring_coverage.refresh_monitoring_coverage",
        "app.tasks.monitoring_cleanup.sync_inventory_to_monitoring",
        "app.tasks.channel_health.observe_channel_health",
        "app.tasks.network_operation_dispatch.publish_network_operation_dispatches",
        "app.tasks.ont_commissioning.reconcile_intents",
        "app.tasks.tr069.reconcile_command_outcomes",
    }
)

EVENT_DRIVEN_TRANSPORT_TASKS = frozenset(
    {
        "app.tasks.radius_population.refresh_radius_from_subs",
        "app.tasks.tr069.execute_network_operation_job",
    }
)


def is_permanent_lifecycle_task(task_name: str) -> bool:
    return task_name in PERMANENT_LIFECYCLE_TASKS


def is_event_driven_transport_task(task_name: str) -> bool:
    """Return whether a task may be requested but not scheduled independently."""
    return task_name in EVENT_DRIVEN_TRANSPORT_TASKS


def _reject_permanent_task_mutation() -> None:
    raise HTTPException(
        status_code=409,
        detail="Core lifecycle tasks cannot be disabled, renamed, or deleted",
    )


def _reject_event_driven_transport_schedule() -> None:
    raise HTTPException(
        status_code=409,
        detail=(
            "Event-driven transport tasks cannot be scheduled independently; "
            "their owning reconciler requests delivery when authoritative drift exists"
        ),
    )


def _validate_schedule_type(value):
    if value is None:
        return None
    if isinstance(value, ScheduleType):
        return value
    try:
        return ScheduleType(value)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid schedule_type") from exc


class ScheduledTasks(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: ScheduledTaskCreate):
        if payload.interval_seconds < 1:
            raise HTTPException(status_code=400, detail="interval_seconds must be >= 1")
        if is_event_driven_transport_task(payload.task_name):
            _reject_event_driven_transport_schedule()
        if is_permanent_lifecycle_task(payload.task_name) and not payload.enabled:
            _reject_permanent_task_mutation()
        task = ScheduledTask(**payload.model_dump())
        db.add(task)
        db.commit()
        db.refresh(task)
        return task

    @staticmethod
    def get(db: Session, task_id: str):
        task = db.get(ScheduledTask, task_id)
        if not task:
            raise HTTPException(status_code=404, detail="Scheduled task not found")
        return task

    @staticmethod
    def list(
        db: Session,
        enabled: bool | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(ScheduledTask)
        if enabled is not None:
            query = query.filter(ScheduledTask.enabled == enabled)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": ScheduledTask.created_at, "name": ScheduledTask.name},
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, task_id: str, payload: ScheduledTaskUpdate):
        task = db.get(ScheduledTask, task_id)
        if not task:
            raise HTTPException(status_code=404, detail="Scheduled task not found")
        data = payload.model_dump(exclude_unset=True)
        if is_permanent_lifecycle_task(task.task_name):
            if data.get("enabled") is False:
                _reject_permanent_task_mutation()
            if "task_name" in data and data["task_name"] != task.task_name:
                _reject_permanent_task_mutation()
        resulting_task_name = data.get("task_name", task.task_name)
        resulting_enabled = data.get("enabled", task.enabled)
        if is_event_driven_transport_task(resulting_task_name) and resulting_enabled:
            _reject_event_driven_transport_schedule()
        if "schedule_type" in data:
            data["schedule_type"] = _validate_schedule_type(data["schedule_type"])
        if "interval_seconds" in data and data["interval_seconds"] is not None:
            if data["interval_seconds"] < 1:
                raise HTTPException(
                    status_code=400, detail="interval_seconds must be >= 1"
                )
        for key, value in data.items():
            setattr(task, key, value)
        db.commit()
        db.refresh(task)
        return task

    @staticmethod
    def delete(db: Session, task_id: str):
        task = db.get(ScheduledTask, task_id)
        if not task:
            raise HTTPException(status_code=404, detail="Scheduled task not found")
        if is_permanent_lifecycle_task(task.task_name):
            _reject_permanent_task_mutation()
        db.delete(task)
        db.commit()


scheduled_tasks = ScheduledTasks()


def refresh_schedule() -> dict:
    return {"detail": "Celery beat refreshes schedules automatically."}


def enqueue_task(task_name: str, args: list | None, kwargs: dict | None) -> dict:
    from app.services.queue_adapter import enqueue_task as enqueue_background_task

    async_result = enqueue_background_task(
        task_name,
        args=args or [],
        kwargs=kwargs or {},
        correlation_id=f"scheduled_task:{task_name}",
        source="scheduler_service",
    )
    return {"queued": True, "task_id": str(async_result.task_id or "")}


def enqueue_by_id(db: Session, task_id: str) -> dict:
    """Look up a scheduled task and enqueue it."""
    task = scheduled_tasks.get(db, task_id)
    return enqueue_task(task.task_name, task.args_json or [], task.kwargs_json or {})
