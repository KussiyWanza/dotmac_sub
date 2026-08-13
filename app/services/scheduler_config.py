import logging
import os
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta

from celery.schedules import crontab

from app.models.domain_settings import SettingDomain
from app.models.scheduler import ScheduledTask, ScheduleType
from app.services import control_registry
from app.services import integration as integration_service
from app.services.db_session_adapter import db_session_adapter
from app.services.scheduler import is_event_driven_transport_task
from app.services.settings_spec import (
    SCHEDULER_BOOLEAN_SETTING_KEYS,
    resolve_boolean,
    resolve_integer,
    resolve_string,
)
from app.timezone import APP_TIMEZONE_NAME

logger = logging.getLogger(__name__)
SessionLocal = db_session_adapter.create_session

TR069_TASK_QUEUE_NAMES = {
    "app.tasks.tr069.sync_all_acs_devices",
    "app.tasks.tr069.reconcile_command_outcomes",
    "app.tasks.tr069.check_device_health",
    "app.tasks.tr069.refresh_ont_runtime_data",
    "app.tasks.tr069.cleanup_tr069_records",
    "app.tasks.tr069.cleanup_stale_genieacs_tasks",
    "app.tasks.tr069.scrape_genieacs_metrics",
    "app.tasks.tr069.execute_network_operation_job",
    "app.tasks.tr069.wait_for_ont_bootstrap",
    "app.tasks.tr069.apply_saved_ont_service_config",
    "app.tasks.tr069.apply_acs_config",
    "app.tasks.ont_commissioning.reconcile_intents",
}


def _scheduler_setting_enabled(db, domain: SettingDomain, key: str) -> bool:
    """Resolve one registered DB-authoritative scheduler setting.

    Optional product capabilities use ``control_registry.is_enabled`` directly.
    This helper is only for operational scheduler controls that have an explicit
    boolean ``SettingSpec``. Ad-hoc environment/database/default fallback is
    deliberately forbidden so a new scheduler boolean cannot silently create a
    second control plane.
    """

    if (domain, key) not in SCHEDULER_BOOLEAN_SETTING_KEYS:
        raise RuntimeError(
            f"Scheduler boolean must be registered: {domain.value}.{key}"
        )
    return resolve_boolean(db, domain, key)


def _sync_scheduled_task(
    db,
    name: str,
    task_name: str,
    enabled: bool,
    interval_seconds: int,
) -> None:
    # Match by NAME (the stable logical identity), not task_name. Matching by
    # task_name meant a task rename/move (e.g. run_dunning -> run_billing_
    # enforcement) found no row and INSERTED a new one, leaving the old row as a
    # duplicate name. Matching by name updates the task_name in place instead.
    tasks = list(
        db.query(ScheduledTask)
        .filter(ScheduledTask.name == name)
        .order_by(ScheduledTask.created_at.desc())
        .all()
    )
    task = tasks[0] if tasks else None
    if not task:
        if not enabled:
            return
        task = ScheduledTask(
            name=name,
            task_name=task_name,
            schedule_type=ScheduleType.interval,
            interval_seconds=interval_seconds,
            enabled=True,
        )
        db.add(task)
        db.commit()
        return
    changed = False
    # Defensive dedupe: delete any stray duplicate rows for this name (the
    # unique constraint prevents new ones, but pre-existing data may have them).
    # Intentional history drop: the surplus rows are hard-deleted; nothing has a
    # FK to scheduled_tasks.id, so no dependent records are affected.
    for duplicate in tasks[1:]:
        db.delete(duplicate)
        changed = True
    if task.task_name != task_name:
        task.task_name = task_name
        changed = True
    if task.interval_seconds != interval_seconds:
        task.interval_seconds = interval_seconds
        changed = True
    if task.enabled != enabled:
        task.enabled = enabled
        changed = True
    if changed:
        db.commit()


def sync_erp_operational_schedule(db, *, enabled: bool) -> None:
    """Apply the capability-owned ERP operational cadence immediately."""

    _sync_scheduled_task(
        db,
        name="dotmac_erp_operational_domain_sync",
        task_name="app.tasks.dotmac_erp_outbox.sync_erp_operational_domains",
        enabled=enabled,
        interval_seconds=300,
    )


def _retire_scheduled_task(db, task_name: str) -> None:
    tasks = db.query(ScheduledTask).filter(ScheduledTask.task_name == task_name).all()
    changed = False
    for task in tasks:
        if task.enabled:
            task.enabled = False
            changed = True
    if changed:
        db.commit()


def find_unregistered_scheduled_tasks(
    registered_task_names: Iterable[str],
) -> list[dict[str, object]]:
    registered = set(registered_task_names)
    session = SessionLocal()
    try:
        tasks = (
            session.query(ScheduledTask)
            .filter(ScheduledTask.enabled.is_(True))
            .order_by(ScheduledTask.name.asc(), ScheduledTask.created_at.asc())
            .all()
        )
        return [
            {
                "id": task.id,
                "name": task.name,
                "task_name": task.task_name,
                "interval_seconds": task.interval_seconds,
            }
            for task in tasks
            if task.task_name not in registered
        ]
    finally:
        session.close()


def get_celery_config() -> dict:
    # Broker endpoints are deployment transport configuration. They must be
    # available before application bootstrap can seed database settings and are
    # therefore environment-owned rather than mutable DomainSetting values.
    redis_url = os.getenv("REDIS_URL")
    broker = os.getenv("CELERY_BROKER_URL") or redis_url or "redis://localhost:6379/0"
    backend = (
        os.getenv("CELERY_RESULT_BACKEND") or redis_url or "redis://localhost:6379/1"
    )
    timezone = APP_TIMEZONE_NAME
    beat_max_loop_interval = 5
    beat_refresh_seconds = 300
    task_soft_time_limit = 840
    task_time_limit = 900
    acs_soft_time_limit = 240
    acs_time_limit = 300
    long_soft_time_limit = 1740
    long_time_limit = 1800
    worker_prefetch_multiplier = 1
    result_expires = 86400
    session = SessionLocal()
    try:
        timezone = resolve_string(session, SettingDomain.scheduler, "timezone")
        beat_max_loop_interval = resolve_integer(
            session, SettingDomain.scheduler, "beat_max_loop_interval"
        )
        beat_refresh_seconds = resolve_integer(
            session, SettingDomain.scheduler, "beat_refresh_seconds"
        )
        task_soft_time_limit = resolve_integer(
            session, SettingDomain.scheduler, "task_soft_time_limit_seconds"
        )
        task_time_limit = resolve_integer(
            session, SettingDomain.scheduler, "task_time_limit_seconds"
        )
        acs_soft_time_limit = resolve_integer(
            session, SettingDomain.scheduler, "acs_task_soft_time_limit_seconds"
        )
        acs_time_limit = resolve_integer(
            session, SettingDomain.scheduler, "acs_task_time_limit_seconds"
        )
        long_soft_time_limit = resolve_integer(
            session, SettingDomain.scheduler, "long_task_soft_time_limit_seconds"
        )
        long_time_limit = resolve_integer(
            session, SettingDomain.scheduler, "long_task_time_limit_seconds"
        )
        worker_prefetch_multiplier = resolve_integer(
            session, SettingDomain.scheduler, "worker_prefetch_multiplier"
        )
        result_expires = resolve_integer(
            session, SettingDomain.scheduler, "result_expires_seconds"
        )
    except Exception:
        logger.exception("Failed to load scheduler settings from database.")
    finally:
        session.close()

    config: dict[str, object] = {
        "broker_url": broker,
        "result_backend": backend,
        "timezone": timezone,
    }
    config["beat_max_loop_interval"] = beat_max_loop_interval
    config["beat_refresh_seconds"] = beat_refresh_seconds
    config["task_soft_time_limit"] = max(30, task_soft_time_limit)
    config["task_time_limit"] = max(config["task_soft_time_limit"] + 1, task_time_limit)
    config["worker_prefetch_multiplier"] = max(1, worker_prefetch_multiplier)
    config["task_acks_late"] = True
    config["task_reject_on_worker_lost"] = True
    config["task_track_started"] = True
    config["broker_connection_retry_on_startup"] = True
    config["worker_cancel_long_running_tasks_on_connection_loss"] = True
    config["result_expires"] = max(60, result_expires)

    acs_limits = {
        "soft_time_limit": max(30, acs_soft_time_limit),
        "time_limit": max(acs_soft_time_limit + 1, acs_time_limit),
    }
    long_limits = {
        "soft_time_limit": max(60, long_soft_time_limit),
        "time_limit": max(long_soft_time_limit + 1, long_time_limit),
    }
    annotations: dict[str, dict[str, int]] = dict.fromkeys(
        TR069_TASK_QUEUE_NAMES, acs_limits
    )
    annotations.update(
        {
            "app.tasks.ont_provisioning.provision_ont": long_limits,
            "app.tasks.ont_provisioning.queue_bulk_provisioning": long_limits,
            "app.tasks.olt_firmware.upgrade_with_verification": long_limits,
            "app.tasks.olt_firmware.rollback": long_limits,
            "app.tasks.provisioning.run_bulk_activation_job": long_limits,
            "app.tasks.provisioning.run_service_migration_job": long_limits,
            # Whole-base daily runs (4k+ subscriptions); the default 900s
            # limit can kill a catch-up billing or dunning pass mid-run.
            "app.tasks.billing.run_invoice_cycle": long_limits,
            "app.tasks.collections.run_billing_enforcement": long_limits,
        }
    )
    config["task_annotations"] = annotations
    return config


def _entry_expires_seconds(interval_seconds: int) -> int:
    """Message expiry for a periodic task.

    A periodic message that hasn't been consumed within its own interval is
    obsolete — its successor is already queued behind it. Expiring it lets a
    backlogged worker discard stale duplicates instead of executing hours of
    identical runs back to back (the default queue once held 115 queued copies
    of one cache refresh). Day-or-longer tasks get 12h: still ample room for
    queue latency, but a dead queue can't accumulate days of business runs.
    """
    if interval_seconds >= 86400:
        return 43200
    return interval_seconds


def _interval_to_beat_schedule(task_id, interval_seconds: int):
    """Beat schedule object for an interval task.

    Celery beat measures `timedelta` intervals from its own (non-persisted)
    start time, so a task only comes due after a full interval of
    uninterrupted beat uptime — under frequent deploys long intervals never
    fire. This starved billing, dunning, expiration and FUP runs for weeks,
    and later starved the hourly topology_lldp_poll/topology_reconcile
    (network_topology_links stayed empty for 17+ days because beat rarely
    survived a full hour between deploys). Intervals that map onto a
    wall-clock cadence are therefore anchored to stable crontab slots,
    spread deterministically by task id so runners don't stampede:

    - day-long: a fixed minute/hour in the 00:00-05:59 window;
    - whole-hour multiples (1h..23h): a fixed minute, every Nth hour.

    Sub-hourly intervals keep their timedelta (a restart delays them by at
    most one interval, which at that cadence is fine), as do intervals that
    don't divide into whole hours (crontab cannot express e.g. a 90-minute
    cadence without drift).
    """
    anchor = task_id.int if hasattr(task_id, "int") else abs(hash(task_id))
    if 86400 <= interval_seconds < 2 * 86400:
        return crontab(minute=anchor % 60, hour=(anchor // 60) % 6)
    if 3600 <= interval_seconds < 86400 and interval_seconds % 3600 == 0:
        step_hours = interval_seconds // 3600
        return crontab(minute=anchor % 60, hour=f"*/{step_hours}")
    if interval_seconds >= 2 * 86400:
        logger.warning(
            "scheduled_task_multiday_interval_restart_relative",
            extra={"task_id": str(task_id), "interval_seconds": interval_seconds},
        )
    return timedelta(seconds=interval_seconds)


def _cron_to_beat_schedule(cron_expr: str | None):
    """Parse a standard 5-field cron expression into a celery ``crontab``.

    Fields are ``minute hour day-of-month month day-of-week``. Returns ``None``
    for a missing/malformed expression (celery validates each field and raises
    on bad syntax), so callers skip the entry rather than crash the scheduler.
    """
    if not cron_expr:
        return None
    fields = str(cron_expr).split()
    if len(fields) != 5:
        return None
    minute, hour, day_of_month, month_of_year, day_of_week = fields
    try:
        return crontab(
            minute=minute,
            hour=hour,
            day_of_month=day_of_month,
            month_of_year=month_of_year,
            day_of_week=day_of_week,
        )
    except Exception:
        return None


def is_valid_cron(cron_expr: str | None) -> bool:
    """Whether ``cron_expr`` is a usable 5-field cron expression."""
    return _cron_to_beat_schedule(cron_expr) is not None


def next_cron_run(cron_expr: str | None):
    """Best-effort next fire time (UTC) from now for a cron expr, or None.

    Uses celery's ``crontab.remaining_estimate`` which is relative to the real
    current time, so this is a live preview, not a pure function of an argument.
    """
    sched = _cron_to_beat_schedule(cron_expr)
    if sched is None:
        return None
    try:
        now = datetime.now(UTC)
        return now + sched.remaining_estimate(now)
    except Exception:
        return None


def _scheduled_row_to_entry(task) -> tuple[str, dict] | None:
    """Build a beat-schedule (key, entry) for a ScheduledTask row, or None to skip.

    Honours ``schedule_type``: ``crontab`` rows use ``cron_expr`` (skipped with a
    warning if malformed); ``interval`` rows use the interval anchoring. Pure (no
    DB) so it can be unit-tested against in-memory rows.
    """
    if is_event_driven_transport_task(task.task_name):
        logger.warning(
            "scheduled_task_event_transport_rejected",
            extra={
                "task_id": str(task.id),
                "task_name": task.task_name,
            },
        )
        return None
    options: dict = {}
    if task.task_name in TR069_TASK_QUEUE_NAMES:
        options["queue"] = "acs"
    if task.schedule_type == ScheduleType.crontab:
        task_schedule = _cron_to_beat_schedule(task.cron_expr)
        if task_schedule is None:
            logger.warning(
                "scheduled_task_invalid_cron",
                extra={
                    "task_id": str(task.id),
                    "task_name": task.task_name,
                    "cron_expr": task.cron_expr,
                },
            )
            return None
    elif task.schedule_type == ScheduleType.interval:
        interval_seconds = max(task.interval_seconds or 0, 1)
        options["expires"] = _entry_expires_seconds(interval_seconds)
        task_schedule = _interval_to_beat_schedule(task.id, interval_seconds)
    else:
        return None
    return f"scheduled_task_{task.id}", {
        "task": task.task_name,
        "schedule": task_schedule,
        "args": task.args_json or [],
        "kwargs": task.kwargs_json or {},
        "options": options,
    }


def build_beat_schedule() -> dict:
    schedule: dict[str, dict] = {}
    session = SessionLocal()
    try:
        enabled = control_registry.is_enabled(session, "gis.sync")
        interval_minutes = resolve_integer(
            session, SettingDomain.gis, "sync_interval_minutes"
        )
        if enabled:
            schedule["gis_sync"] = {
                "task": "app.tasks.gis.sync_gis_sources",
                "schedule": timedelta(minutes=max(interval_minutes, 1)),
            }
        credential_rotation_enabled = _scheduler_setting_enabled(
            session,
            SettingDomain.auth,
            "credential_rotation_enabled",
        )
        _sync_scheduled_task(
            session,
            name="credential_encryption_key_rotation",
            task_name="app.tasks.security.run_scheduled_credential_rotation",
            enabled=credential_rotation_enabled,
            interval_seconds=86400,
        )
        usage_admission_enabled = _scheduler_setting_enabled(
            session,
            SettingDomain.usage,
            "usage_rating_enabled",
        )
        usage_interval_seconds = resolve_integer(
            session, SettingDomain.usage, "usage_rating_interval_seconds"
        )
        usage_interval_seconds = max(usage_interval_seconds, 300)
        usage_metering_interval_seconds = resolve_integer(
            session, SettingDomain.usage, "usage_metering_interval_seconds"
        )
        usage_metering_interval_seconds = max(usage_metering_interval_seconds, 60)
        fup_evaluation_interval_seconds = resolve_integer(
            session, SettingDomain.usage, "fup_evaluation_interval_seconds"
        )
        fup_evaluation_interval_seconds = max(fup_evaluation_interval_seconds, 60)
        _sync_scheduled_task(
            session,
            name="usage_rating_runner",
            task_name="app.tasks.usage.run_usage_rating",
            enabled=usage_admission_enabled,
            interval_seconds=usage_interval_seconds,
        )
        radius_accounting_enabled = control_registry.is_enabled(
            session, "sessions.radius_accounting_import"
        )
        radius_accounting_interval_seconds = resolve_integer(
            session, SettingDomain.usage, "radius_accounting_import_interval_seconds"
        )
        radius_accounting_interval_seconds = max(radius_accounting_interval_seconds, 10)
        _sync_scheduled_task(
            session,
            name="radius_accounting_importer",
            task_name="app.tasks.usage.import_radius_accounting",
            enabled=radius_accounting_enabled,
            interval_seconds=radius_accounting_interval_seconds,
        )
        # Close ghost sessions (no Stop packet, accounting feed silent). Off by
        # default: enable only after the importer's open-session refresh has
        # been running for at least one interim interval, so genuinely live
        # sessions have a fresh last_update_at before the first reap.
        radius_reap_enabled = control_registry.is_enabled(
            session, "sessions.radius_reap_stale"
        )
        radius_reap_interval_seconds = resolve_integer(
            session, SettingDomain.usage, "radius_session_reap_interval_seconds"
        )
        radius_reap_interval_seconds = max(radius_reap_interval_seconds, 60)
        # Daily heads-up before a purchased data bundle lapses (push + email).
        _sync_scheduled_task(
            session,
            name="data_bundle_expiry_notifier",
            task_name="app.tasks.usage.notify_expiring_data_bundles",
            enabled=usage_admission_enabled,
            interval_seconds=86400,
        )
        _sync_scheduled_task(
            session,
            name="radius_session_reaper",
            task_name="app.tasks.usage.reap_stale_radius_sessions",
            enabled=radius_reap_enabled,
            interval_seconds=radius_reap_interval_seconds,
        )
        # Staff device-login reconcile: authoritative sweep of radcheck_admin /
        # radreply_admin from active SystemUser device-login state. This is the
        # backstop that actually revokes router login when staff are deactivated,
        # deleted, or renamed — populate_device_login otherwise only runs on a
        # device-login edit, so a deactivation via the normal user-admin path
        # would never take effect in RADIUS. This security projection repair is
        # permanent; disabling new usage work cannot preserve stale access.
        device_login_sync_interval_seconds = max(
            resolve_integer(
                session, SettingDomain.usage, "device_login_sync_interval_seconds"
            ),
            60,
        )
        _sync_scheduled_task(
            session,
            name="device_login_sync",
            task_name="app.tasks.radius_population.sync_device_login",
            enabled=True,
            interval_seconds=device_login_sync_interval_seconds,
        )
        # Companion to the app-side reaper above: this one closes the *external*
        # FreeRADIUS radacct table (the mirror reaper only touches the app-side
        # RadiusAccountingSession). Same flag/interval — without it, dead NAS
        # leave phantom radacct "online" sessions forever.
        _sync_scheduled_task(
            session,
            name="radacct_ghost_reaper",
            task_name="app.tasks.radius.reap_radacct_ghosts",
            enabled=radius_reap_enabled,
            interval_seconds=radius_reap_interval_seconds,
        )
        # Rebuild the live radius_active_sessions view from OPEN external radacct
        # sessions. The accounting-hook populator isn't firing in prod (the table
        # starved to 1 row while radacct carries ~893 open sessions), so this
        # discover-reconcile sweep is the authoritative populator. Projection
        # repair remains permanent even when new usage/FUP work is disabled.
        radius_active_session_reconcile_interval_seconds = max(
            resolve_integer(
                session,
                SettingDomain.usage,
                "radius_active_session_reconcile_interval_seconds",
            ),
            60,
        )
        _sync_scheduled_task(
            session,
            name="radius_active_session_reconcile",
            task_name="app.tasks.radius.reconcile_active_sessions",
            enabled=True,
            interval_seconds=radius_active_session_reconcile_interval_seconds,
        )
        # Enforce billing suspensions at the live session (SP-1). A suspend only
        # writes Auth-Type := Reject, which bites at the *next* re-auth — a stable
        # PPPoE session survives it, so a suspended/terminated subscriber can stay
        # online for days (revenue leak). run_enforcement_reconciler CoA-kicks open
        # sessions whose user has no radcheck row or sits in a reject pool, and
        # repairs exact RADIUS row drift. It caps disconnect attempts per run so
        # transport failure or systemic drift degrades to alerts rather than an
        # unbounded retry or mass disconnect — hence safe to run continuously as
        # a mandatory lifecycle task.
        enforcement_reconciler_interval_seconds = max(
            resolve_integer(
                session, SettingDomain.usage, "enforcement_reconciler_interval_seconds"
            ),
            120,
        )
        _sync_scheduled_task(
            session,
            name="enforcement_reconciler",
            task_name="app.tasks.radius.run_enforcement_reconciler",
            enabled=True,
            interval_seconds=enforcement_reconciler_interval_seconds,
        )
        # Account and external access projections share this recovery loop.
        # Keep exactly one scheduled owner for access convergence.
        _retire_scheduled_task(
            session, "app.tasks.enforcement.reconcile_account_status_drift"
        )
        # Billing approval is an admission fact, not a runtime bypass. This
        # permanent owner pass repairs the impossible active + unapproved split
        # state through the account lifecycle coordinator.
        _sync_scheduled_task(
            session,
            name="billing_approval_reconcile",
            task_name="app.tasks.enforcement.reconcile_billing_approval_drift",
            enabled=True,
            interval_seconds=900,
        )
        # The full RADIUS projection writer remains an event-driven transport.
        # This permanent reconciler compares desired-versus-observed state and
        # requests that writer only when repair is required.
        _retire_scheduled_task(
            session, "app.tasks.radius_population.refresh_radius_from_subs"
        )
        # stale overdue locks: money-adjacent, so DETECT-only (dry-run) — it
        # WARNs with the candidate count for an operator to clear after review,
        # rather than auto-clearing an overdue lock on a possibly-wrong "no debt".
        stale_overdue_lock_detect_enabled = _scheduler_setting_enabled(
            session,
            SettingDomain.billing,
            "stale_overdue_lock_detect_enabled",
        )
        _sync_scheduled_task(
            session,
            name="stale_overdue_lock_detect",
            task_name="app.tasks.enforcement.detect_stale_overdue_locks",
            enabled=stale_overdue_lock_detect_enabled,
            interval_seconds=86400,
        )
        # Cross-app drift detector (R-3): daily read-only proof that CRM / sub /
        # ERP still agree on the business facts that matter. Detect-only — it
        # persists findings by fingerprint and WARNs on material drift, pointing
        # at the reconciler that owns each fix; it never heals. Safe to run by
        # default (no writes to business state).
        cross_app_drift_enabled = _scheduler_setting_enabled(
            session,
            SettingDomain.audit,
            "cross_app_drift_detection_enabled",
        )
        _sync_scheduled_task(
            session,
            name="cross_app_drift_detection",
            task_name="app.tasks.cross_app_drift.run_cross_app_drift_detection",
            enabled=cross_app_drift_enabled,
            interval_seconds=86400,
        )
        # Roll imported RADIUS accounting into quota buckets (feeds FUP/overage).
        # Gated by the same usage flag. This follows the RADIUS accounting
        # cadence instead of the daily usage-rating cadence so FUP decisions
        # are applied within minutes of imported usage.
        _sync_scheduled_task(
            session,
            name="usage_metering_runner",
            task_name="app.tasks.usage.meter_usage_into_quota",
            enabled=usage_admission_enabled,
            interval_seconds=usage_metering_interval_seconds,
        )
        # Evaluate FUP rules against the metered usage and apply / auto-lift
        # throttle/block. Keep this separate from daily usage rating; capped
        # plans need near-real-time enforcement.
        _sync_scheduled_task(
            session,
            name="fup_evaluation_runner",
            task_name="app.tasks.usage.evaluate_fup_rules",
            enabled=usage_admission_enabled,
            interval_seconds=fup_evaluation_interval_seconds,
        )
        # Queue-independent backstop: lift FUP enforcement whose reset boundary
        # has passed even if the billing queue (where evaluate_fup_rules runs) is
        # stalled, so customers aren't left throttled/blocked past their window.
        _sync_scheduled_task(
            session,
            name="fup_reset_safety_net",
            task_name="app.tasks.usage.lift_expired_fup_enforcement",
            enabled=True,
            interval_seconds=max(usage_interval_seconds, 300),
        )
        # Zabbix portal-usage ingestion retired with the native monitoring
        # cutover; disable any existing DB rows.
        _retire_scheduled_task(
            session,
            "app.tasks.zabbix_ingestion.ingest_portal_usage",
        )
        _retire_scheduled_task(
            session,
            "app.tasks.zabbix_ingestion.dispatch_portal_usage_ingestion",
        )
        billing_interval_seconds = resolve_integer(
            session, SettingDomain.billing, "billing_interval_seconds"
        )
        billing_interval_seconds = max(billing_interval_seconds, 300)
        _sync_scheduled_task(
            session,
            name="billing_runner",
            task_name="app.tasks.billing.run_invoice_cycle",
            enabled=True,
            interval_seconds=billing_interval_seconds,
        )
        compensation_retry_interval_seconds = resolve_integer(
            session, SettingDomain.provisioning, "compensation_retry_interval_seconds"
        )
        compensation_retry_interval_seconds = max(
            compensation_retry_interval_seconds,
            60,
        )
        _sync_scheduled_task(
            session,
            name="compensation_retry_watchdog",
            task_name="app.tasks.provisioning.retry_pending_compensation_failures",
            enabled=True,
            interval_seconds=compensation_retry_interval_seconds,
        )
        # Overdue invoice detection — independent of billing cycle
        overdue_interval = resolve_integer(
            session, SettingDomain.billing, "overdue_check_interval_seconds"
        )
        overdue_interval = max(overdue_interval, 300)
        _sync_scheduled_task(
            session,
            name="overdue_checker",
            task_name="app.tasks.billing.mark_invoices_overdue",
            enabled=True,
            interval_seconds=overdue_interval,
        )
        # The permanent hourly notifications runner owns reminders and honours
        # the configured customer-message send window.
        billing_notif_interval = resolve_integer(
            session, SettingDomain.collections, "billing_notifications_interval_seconds"
        )
        billing_notif_interval = max(billing_notif_interval, 300)
        _sync_scheduled_task(
            session,
            name="billing_notifications_runner",
            task_name="app.tasks.billing.run_billing_notifications",
            enabled=True,
            interval_seconds=billing_notif_interval,
        )
        # Unified billing enforcement. Accrual remains mode-specific;
        # suspension/restore decisions converge in the owner.
        dunning_interval_seconds = resolve_integer(
            session, SettingDomain.collections, "dunning_interval_seconds"
        )
        dunning_interval_seconds = max(dunning_interval_seconds, 60)
        _sync_scheduled_task(
            session,
            name="dunning_runner",
            task_name="app.tasks.collections.run_billing_enforcement",
            enabled=True,
            interval_seconds=dunning_interval_seconds,
        )
        # Bundle-state reconcile: heal any divergent bundle member (base active
        # while a component sub is suspended, or vice versa) back to its anchor.
        _sync_scheduled_task(
            session,
            name="bundle_reconcile",
            task_name="app.tasks.collections.run_bundle_reconcile",
            enabled=True,
            interval_seconds=900,
        )
        # Balance/expiry-based prepaid enforcement sweep. Funding, coverage,
        # quarantine, shields, grace, and the shared time-of-day window decide
        # each account; there is no global enablement control.
        prepaid_balance_sweep_interval_seconds = resolve_integer(
            session, SettingDomain.collections, "prepaid_balance_sweep_interval_seconds"
        )
        _sync_scheduled_task(
            session,
            name="prepaid_balance_sweep",
            task_name="app.tasks.collections.prepaid_balance_sweep",
            enabled=True,
            interval_seconds=prepaid_balance_sweep_interval_seconds,
        )
        # Customer-financial lifecycle health observation.
        _sync_scheduled_task(
            session,
            name="billing_switch_guard",
            task_name="app.tasks.billing.check_billing_switch",
            enabled=True,
            interval_seconds=3600,
        )
        # Heavy cohort health is produced out of band. Prometheus scrapes only
        # the bounded Redis snapshot and never walks customer financial rows.
        _sync_scheduled_task(
            session,
            name="billing_health_snapshot",
            task_name="app.tasks.billing.refresh_billing_health_snapshot",
            enabled=True,
            interval_seconds=900,
        )
        cutover_audit_enabled = _scheduler_setting_enabled(
            session,
            SettingDomain.billing,
            "cutover_balance_audit_enabled",
        )
        cutover_audit_interval_seconds = resolve_integer(
            session, SettingDomain.billing, "cutover_balance_audit_interval_seconds"
        )
        _sync_scheduled_task(
            session,
            name="cutover_balance_invariant_audit",
            task_name="app.tasks.billing.audit_cutover_balance_invariant",
            enabled=cutover_audit_enabled,
            interval_seconds=max(cutover_audit_interval_seconds, 3600),
        )
        funded_inactive_audit_enabled = _scheduler_setting_enabled(
            session,
            SettingDomain.billing,
            "funded_inactive_exposure_audit_enabled",
        )
        funded_inactive_audit_interval_seconds = resolve_integer(
            session,
            SettingDomain.billing,
            "funded_inactive_exposure_audit_interval_seconds",
        )
        _sync_scheduled_task(
            session,
            name="funded_inactive_exposure_audit",
            task_name="app.tasks.billing.audit_funded_inactive_exposure",
            enabled=funded_inactive_audit_enabled,
            interval_seconds=max(funded_inactive_audit_interval_seconds, 86400),
        )
        # Autopay charging (idempotent; due-date gating lives in the service)
        autopay_interval_seconds = resolve_integer(
            session, SettingDomain.billing, "autopay_interval_seconds"
        )
        autopay_interval_seconds = max(autopay_interval_seconds, 300)
        _sync_scheduled_task(
            session,
            name="autopay_runner",
            task_name="app.tasks.autopay.charge_due_invoices",
            enabled=True,
            interval_seconds=autopay_interval_seconds,
        )
        # Payment arrangement installment due/overdue/default checks (daily)
        arrangement_check_interval_seconds = resolve_integer(
            session, SettingDomain.collections, "arrangement_check_interval_seconds"
        )
        arrangement_check_interval_seconds = max(
            arrangement_check_interval_seconds, 3600
        )
        _sync_scheduled_task(
            session,
            name="arrangement_overdue_checker",
            task_name="app.tasks.arrangements.check_overdue_arrangements",
            enabled=True,
            interval_seconds=arrangement_check_interval_seconds,
        )
        # Sweep stranded top-up intents against the gateway verify API
        topup_reconciliation_interval_seconds = resolve_integer(
            session, SettingDomain.billing, "topup_reconciliation_interval_seconds"
        )
        topup_reconciliation_interval_seconds = max(
            topup_reconciliation_interval_seconds, 300
        )
        _sync_scheduled_task(
            session,
            name="topup_reconciliation_runner",
            task_name="app.tasks.payment_reconciliation.reconcile_topups",
            enabled=True,
            interval_seconds=topup_reconciliation_interval_seconds,
        )
        # Suspension-enforcement audit — read-only check that fully-blocked
        # subscribers are actually unreachable in the external RADIUS DB.
        suspension_audit_enabled = _scheduler_setting_enabled(
            session,
            SettingDomain.radius,
            "suspension_audit_enabled",
        )
        suspension_audit_interval_seconds = resolve_integer(
            session, SettingDomain.radius, "suspension_audit_interval_seconds"
        )
        suspension_audit_interval_seconds = max(suspension_audit_interval_seconds, 900)
        _sync_scheduled_task(
            session,
            name="radius_suspension_audit",
            task_name="app.tasks.radius.audit_suspension_enforcement",
            enabled=suspension_audit_enabled,
            interval_seconds=suspension_audit_interval_seconds,
        )
        # IPv4 consistency audit (read-only; quantifies column/IPAM/radreply
        # drift). Shares cadence defaults with the suspension audit.
        ip_consistency_audit_enabled = _scheduler_setting_enabled(
            session,
            SettingDomain.radius,
            "ip_consistency_audit_enabled",
        )
        ip_consistency_audit_interval_seconds = resolve_integer(
            session, SettingDomain.radius, "ip_consistency_audit_interval_seconds"
        )
        ip_consistency_audit_interval_seconds = max(
            ip_consistency_audit_interval_seconds, 900
        )
        _sync_scheduled_task(
            session,
            name="radius_ip_consistency_audit",
            task_name="app.tasks.radius.audit_ip_consistency",
            enabled=ip_consistency_audit_enabled,
            interval_seconds=ip_consistency_audit_interval_seconds,
        )
        # Connectivity shadow audit (read-only full-base sweep; quantifies
        # desired-vs-actual drift per dimension — the cutover-readiness gauge for
        # the connectivity reconciler). Shares the audit cadence defaults.
        connectivity_shadow_audit_enabled = _scheduler_setting_enabled(
            session,
            SettingDomain.radius,
            "connectivity_shadow_audit_enabled",
        )
        connectivity_shadow_audit_interval_seconds = resolve_integer(
            session, SettingDomain.radius, "connectivity_shadow_audit_interval_seconds"
        )
        connectivity_shadow_audit_interval_seconds = max(
            connectivity_shadow_audit_interval_seconds, 900
        )
        _sync_scheduled_task(
            session,
            name="connectivity_shadow_audit",
            task_name="app.tasks.radius.connectivity_shadow_audit",
            enabled=connectivity_shadow_audit_enabled,
            interval_seconds=connectivity_shadow_audit_interval_seconds,
        )
        # Subscription expiration enforcement (runs daily)
        subscription_expiration_interval_seconds = resolve_integer(
            session, SettingDomain.catalog, "subscription_expiration_interval_seconds"
        )
        subscription_expiration_interval_seconds = max(
            subscription_expiration_interval_seconds, 3600
        )
        _sync_scheduled_task(
            session,
            name="subscription_expiration_runner",
            task_name="app.tasks.catalog.expire_subscriptions",
            enabled=True,
            interval_seconds=subscription_expiration_interval_seconds,
        )
        # Warn BEFORE the expiry runner above cuts service. `send_expiry_reminders`
        # and the `expiry_reminder_days` setting have both existed for a long
        # time, but the task was never scheduled — so subscriptions expired and
        # suspended on time while nobody was warned first, which is the single
        # largest source of "I paid, why am I off?" contacts. Daily is right:
        # the task reminds on a days-before window, so a shorter interval would
        # re-send to the same customers.
        expiry_reminder_interval_seconds = resolve_integer(
            session,
            SettingDomain.catalog,
            "expiry_reminder_interval_seconds",
        )
        expiry_reminder_interval_seconds = max(expiry_reminder_interval_seconds, 3600)
        _sync_scheduled_task(
            session,
            name="subscription_expiry_reminder_runner",
            task_name="app.tasks.catalog.send_expiry_reminders",
            enabled=True,
            interval_seconds=expiry_reminder_interval_seconds,
        )
        # Apply admin-scheduled next-cycle plan changes once their effective
        # (next-billing) date arrives. Hourly so a change lands promptly after
        # the boundary; the applier is idempotent (only picks up approved,
        # unapplied rows whose effective_date has passed).
        scheduled_plan_change_interval_seconds = resolve_integer(
            session, SettingDomain.catalog, "scheduled_plan_change_interval_seconds"
        )
        scheduled_plan_change_interval_seconds = max(
            scheduled_plan_change_interval_seconds, 300
        )
        _sync_scheduled_task(
            session,
            name="scheduled_plan_change_applier",
            task_name="app.tasks.catalog.apply_due_subscription_changes",
            enabled=True,
            interval_seconds=scheduled_plan_change_interval_seconds,
        )
        # Deferred status commands have their own durable queue. Keep this
        # runner independent from plan changes so reviewed access/lifecycle
        # actions cannot be stranded.
        scheduled_status_change_interval_seconds = resolve_integer(
            session, SettingDomain.catalog, "scheduled_status_change_interval_seconds"
        )
        scheduled_status_change_interval_seconds = max(
            scheduled_status_change_interval_seconds, 60
        )
        _sync_scheduled_task(
            session,
            name="scheduled_subscription_status_applier",
            task_name=("app.tasks.catalog.apply_due_subscription_status_commands"),
            enabled=True,
            interval_seconds=scheduled_status_change_interval_seconds,
        )
        # Infrastructure availability snapshot - daily rollup powering the
        # performance/SLA trend charts (see INFRASTRUCTURE_SLA_PERFORMANCE.md).
        # Safe to run regardless of SLA_AVAILABILITY_LOG_ENABLED: it records the
        # day's per-element availability (PON ONT-online ratio + alert-derived
        # device/site uptime), accumulating trend history either way.
        infra_availability_snapshot_enabled = _scheduler_setting_enabled(
            session,
            SettingDomain.network_monitoring,
            "infra_availability_snapshot_enabled",
        )
        infra_availability_snapshot_interval_seconds = resolve_integer(
            session,
            SettingDomain.network_monitoring,
            "infra_availability_snapshot_interval_seconds",
        )
        infra_availability_snapshot_interval_seconds = max(
            infra_availability_snapshot_interval_seconds, 3600
        )
        _sync_scheduled_task(
            session,
            name="infra_availability_snapshot",
            task_name=(
                "app.tasks.infrastructure_availability."
                "snapshot_infrastructure_availability"
            ),
            enabled=infra_availability_snapshot_enabled,
            interval_seconds=infra_availability_snapshot_interval_seconds,
        )
        # Infrastructure availability snapshot retention prune (daily).
        infra_availability_prune_enabled = _scheduler_setting_enabled(
            session,
            SettingDomain.network_monitoring,
            "infra_availability_prune_enabled",
        )
        infra_availability_prune_interval_seconds = resolve_integer(
            session,
            SettingDomain.network_monitoring,
            "infra_availability_prune_interval_seconds",
        )
        infra_availability_prune_interval_seconds = max(
            infra_availability_prune_interval_seconds, 3600
        )
        _sync_scheduled_task(
            session,
            name="infra_availability_prune",
            task_name=(
                "app.tasks.infrastructure_availability."
                "prune_infrastructure_availability"
            ),
            enabled=infra_availability_prune_enabled,
            interval_seconds=infra_availability_prune_interval_seconds,
        )
        # Permanent unified-device projection repair. The projection is the
        # SQL read model for the device list, so accepted canonical device state
        # must continue to converge regardless of mutable feature/settings state.
        device_projection_reconcile_interval_seconds = resolve_integer(
            session,
            SettingDomain.network_monitoring,
            "device_projection_reconcile_interval_seconds",
        )
        device_projection_reconcile_interval_seconds = max(
            device_projection_reconcile_interval_seconds, 60
        )
        _sync_scheduled_task(
            session,
            name="device_projection_reconcile",
            task_name="app.tasks.device_projection.reconcile_device_projections",
            enabled=True,
            interval_seconds=device_projection_reconcile_interval_seconds,
        )
        # Vacation hold auto-resume - runs hourly to resume expired holds
        vacation_hold_resume_interval = resolve_integer(
            session, SettingDomain.catalog, "vacation_hold_resume_interval_seconds"
        )
        vacation_hold_resume_interval = max(vacation_hold_resume_interval, 300)
        _sync_scheduled_task(
            session,
            name="vacation_hold_auto_resume",
            task_name="app.tasks.vacation_holds.resume_expired_holds",
            enabled=True,
            interval_seconds=vacation_hold_resume_interval,
        )
        notification_queue_interval_seconds = resolve_integer(
            session, SettingDomain.notification, "notification_queue_interval_seconds"
        )
        notification_queue_interval_seconds = max(
            notification_queue_interval_seconds, 30
        )
        _sync_scheduled_task(
            session,
            name="notification_queue_runner",
            task_name="app.tasks.notifications.deliver_notification_queue",
            enabled=True,
            interval_seconds=notification_queue_interval_seconds,
        )
        campaign_processing_interval_seconds = max(
            resolve_integer(
                session, SettingDomain.comms, "campaign_processing_interval_seconds"
            ),
            30,
        )
        _sync_scheduled_task(
            session,
            name="campaign_due_runner",
            task_name="app.tasks.campaigns.process_due_campaigns",
            enabled=True,
            interval_seconds=campaign_processing_interval_seconds,
        )
        _sync_scheduled_task(
            session,
            name="campaign_sequence_runner",
            task_name="app.tasks.campaigns.process_due_campaign_steps",
            enabled=True,
            interval_seconds=campaign_processing_interval_seconds,
        )
        operational_escalation_delivery_enabled = _scheduler_setting_enabled(
            session,
            SettingDomain.notification,
            "operational_escalation_delivery_enabled",
        )
        operational_escalation_delivery_interval_seconds = resolve_integer(
            session,
            SettingDomain.notification,
            "operational_escalation_delivery_interval_seconds",
        )
        operational_escalation_delivery_interval_seconds = max(
            operational_escalation_delivery_interval_seconds,
            30,
        )
        _sync_scheduled_task(
            session,
            name="operational_escalation_delivery_runner",
            task_name=(
                "app.tasks.operational_escalations."
                "dispatch_operational_escalation_deliveries"
            ),
            enabled=operational_escalation_delivery_enabled,
            interval_seconds=operational_escalation_delivery_interval_seconds,
        )
        retention_enabled = _scheduler_setting_enabled(
            session,
            SettingDomain.catalog,
            "nas_backup_retention_enabled",
        )
        retention_interval_seconds = resolve_integer(
            session, SettingDomain.provisioning, "nas_backup_retention_interval_seconds"
        )
        retention_interval_seconds = max(retention_interval_seconds, 3600)
        _sync_scheduled_task(
            session,
            name="nas_backup_retention_cleanup",
            task_name="app.tasks.nas.cleanup_nas_backups",
            enabled=retention_enabled,
            interval_seconds=retention_interval_seconds,
        )
        # OAuth token refresh - runs daily to proactively refresh expiring tokens
        oauth_refresh_enabled = _scheduler_setting_enabled(
            session,
            SettingDomain.comms,
            "oauth_token_refresh_enabled",
        )
        oauth_refresh_interval_seconds = resolve_integer(
            session, SettingDomain.provisioning, "oauth_token_refresh_interval_seconds"
        )
        oauth_refresh_interval_seconds = max(
            oauth_refresh_interval_seconds, 3600
        )  # Min: 1 hour
        _sync_scheduled_task(
            session,
            name="oauth_token_refresh",
            task_name="app.tasks.oauth.refresh_expiring_tokens",
            enabled=oauth_refresh_enabled,
            interval_seconds=oauth_refresh_interval_seconds,
        )
        # Team Inbox maintenance. These tasks and their reliability policies
        # already existed but were registered with no schedule at all, so none
        # of them ever ran: a reply the composer reported as "scheduled" was
        # never sent, a failed outbound message never retried itself, and an
        # expired snooze never returned its conversation to the queue.
        _sync_scheduled_task(
            session,
            name="team_inbox_scheduled_reply_release",
            task_name="app.tasks.team_inbox.release_scheduled_replies",
            enabled=True,
            # A send-later reply should leave within a minute of its time; any
            # coarser and the operator's chosen moment is visibly wrong.
            interval_seconds=60,
        )
        # Shadow participant projection. Nothing reads it for a decision yet;
        # it runs so the projection is populated and measurable before any
        # threading or export rule is allowed to depend on it.
        _sync_scheduled_task(
            session,
            name="team_inbox_participant_backfill",
            task_name="app.tasks.team_inbox.backfill_conversation_participants",
            enabled=True,
            interval_seconds=900,
        )
        _sync_scheduled_task(
            session,
            name="team_inbox_snooze_waker",
            task_name="app.tasks.team_inbox.wake_due_snoozed_conversations",
            enabled=True,
            interval_seconds=300,
        )
        _sync_scheduled_task(
            session,
            name="team_inbox_fifo_queue_promotion",
            task_name="app.tasks.team_inbox.promote_queued_conversations",
            enabled=True,
            interval_seconds=60,
        )
        _sync_scheduled_task(
            session,
            name="team_inbox_reply_reminders",
            task_name="app.tasks.team_inbox.send_reply_reminders",
            enabled=True,
            interval_seconds=60,
        )
        _sync_scheduled_task(
            session,
            name="team_inbox_ai_intake_recovery",
            task_name="app.tasks.team_inbox.recover_stale_ai_intake",
            enabled=True,
            interval_seconds=60,
        )
        _sync_scheduled_task(
            session,
            name="team_inbox_failed_outbound_retry",
            task_name="app.tasks.team_inbox.retry_failed_outbound_messages",
            enabled=True,
            interval_seconds=900,
        )
        _sync_scheduled_task(
            session,
            name="team_inbox_media_asset_promotion",
            task_name="app.tasks.team_inbox.promote_message_media_assets",
            enabled=True,
            interval_seconds=900,
        )
        # Auto-resolve is a policy that closes customer conversations, so it
        # stays off until it is deliberately turned on for a tenant.
        _sync_scheduled_task(
            session,
            name="team_inbox_stale_auto_resolve",
            task_name="app.tasks.team_inbox.auto_resolve_stale_conversations",
            enabled=False,
            interval_seconds=3600,
        )
        integration_jobs = integration_service.list_interval_jobs(session)
        if not integration_jobs:
            logger.info("EMAIL_POLL_EXIT reason=no_jobs")
        for job in integration_jobs:
            binding = getattr(job, "capability_binding", None)
            if (
                binding is not None
                and getattr(binding, "capability_id", None)
                == "crm.ticket_observation.v1"
            ):
                logger.info(
                    "integration_interval_job_skipped_dedicated_crm_pull job_id=%s",
                    getattr(job, "id", ""),
                )
                continue
            # Be defensive: tests may use MagicMock jobs and production may have
            # string values depending on where the job was sourced.
            raw_seconds = getattr(job, "interval_seconds", None)
            if isinstance(raw_seconds, (int, float, str)):
                try:
                    interval_seconds = (
                        int(raw_seconds) if raw_seconds is not None else None
                    )
                except (TypeError, ValueError):
                    interval_seconds = None
            else:
                interval_seconds = None

            if interval_seconds is None:
                raw_minutes = getattr(job, "interval_minutes", None)
                if isinstance(raw_minutes, (int, float, str)):
                    try:
                        minutes = int(raw_minutes) if raw_minutes is not None else 0
                    except (TypeError, ValueError):
                        minutes = 0
                else:
                    minutes = 0
                if minutes:
                    interval_seconds = minutes * 60

            interval_seconds = max(interval_seconds or 0, 1)
            schedule[f"integration_job_{job.id}"] = {
                "task": "app.tasks.integrations.run_integration_job",
                "schedule": timedelta(seconds=interval_seconds),
                "args": [str(job.id)],
            }

        # Bandwidth monitoring tasks
        bandwidth_enabled = _scheduler_setting_enabled(
            session,
            SettingDomain.usage,
            "bandwidth_processing_enabled",
        )
        if bandwidth_enabled:
            # Process bandwidth stream - runs every 5 seconds
            bandwidth_stream_interval = resolve_integer(
                session, SettingDomain.bandwidth, "stream_interval_seconds"
            )
            _sync_scheduled_task(
                session,
                name="bandwidth_stream_processor",
                task_name="app.tasks.bandwidth.process_bandwidth_stream",
                enabled=bandwidth_enabled,
                interval_seconds=max(bandwidth_stream_interval, 1),
            )

            # Aggregate to VictoriaMetrics - runs every minute
            aggregate_interval = resolve_integer(
                session, SettingDomain.bandwidth, "aggregate_interval_seconds"
            )
            _sync_scheduled_task(
                session,
                name="bandwidth_aggregate_to_metrics",
                task_name="app.tasks.bandwidth.aggregate_to_metrics",
                enabled=bandwidth_enabled,
                interval_seconds=max(aggregate_interval, 10),
            )

            # Cleanup hot data - runs hourly
            cleanup_interval = resolve_integer(
                session, SettingDomain.bandwidth, "cleanup_interval_seconds"
            )
            _sync_scheduled_task(
                session,
                name="bandwidth_cleanup_hot_data",
                task_name="app.tasks.bandwidth.cleanup_hot_data",
                enabled=bandwidth_enabled,
                interval_seconds=max(cleanup_interval, 60),
            )

            # Trim Redis stream - runs every 10 minutes
            trim_interval = resolve_integer(
                session, SettingDomain.bandwidth, "trim_interval_seconds"
            )
            _sync_scheduled_task(
                session,
                name="bandwidth_trim_stream",
                task_name="app.tasks.bandwidth.trim_redis_stream",
                enabled=bandwidth_enabled,
                interval_seconds=max(trim_interval, 60),
            )

        # Monitoring-path coverage refresh — caches the reachable-CIDR set (from
        # up WireGuard peers) so operational status + the SLA bridge can tell a
        # blind spot from a real outage without running wg on the request path.
        coverage_interval_seconds = resolve_integer(
            session,
            SettingDomain.network_monitoring,
            "monitoring_coverage_interval_seconds",
        )
        _sync_scheduled_task(
            session,
            name="monitoring_coverage_refresh",
            task_name="app.tasks.monitoring_coverage.refresh_monitoring_coverage",
            enabled=True,
            interval_seconds=max(coverage_interval_seconds, 120),
        )

        # OLT health retry - auto-retry failed ping connections
        olt_health_retry_minutes = resolve_integer(
            session,
            SettingDomain.network_monitoring,
            "olt_health_retry_interval_minutes",
        )
        _sync_scheduled_task(
            session,
            name="olt_health_retry",
            task_name="app.tasks.olt_health_retry.retry_failed_olt_connections",
            enabled=True,
            interval_seconds=max(olt_health_retry_minutes * 60, 60),
        )

        # Reap provisioning runs stuck in 'running' (worker died mid-run) so
        # they reach a terminal status instead of blocking dedup + order
        # advancement forever.
        _sync_scheduled_task(
            session,
            name="provisioning_run_reaper",
            task_name="app.tasks.provisioning.reap_stale_provisioning_runs",
            enabled=True,
            interval_seconds=600,
        )

        # Zabbix-fed ONT signal ingest + topology reconcile retired with the
        # native monitoring cutover; disable any existing DB rows.
        _retire_scheduled_task(
            session, "app.tasks.zabbix_ingestion.ingest_olt_signals_from_zabbix"
        )
        _retire_scheduled_task(
            session, "app.tasks.zabbix_ingestion.repair_stale_olt_signal_ingest"
        )
        _retire_scheduled_task(
            session, "app.tasks.topology_sync.run_topology_reconcile"
        )
        # Native infrastructure poll: ping/SNMP reachability sweep over active
        # network devices (Zabbix runtime cutover). The sweep itself only
        # probes devices whose last check is older than the per-check staleness
        # settings, so a tight beat cadence is safe; overlap is single-flighted
        # by an advisory lock inside the task.
        infrastructure_poll_seconds = resolve_integer(
            session,
            SettingDomain.network_monitoring,
            "infrastructure_poll_interval_seconds",
        )
        _sync_scheduled_task(
            session,
            name="infrastructure_poll",
            task_name="app.tasks.infrastructure_polling.run_infrastructure_poll",
            enabled=True,
            interval_seconds=max(infrastructure_poll_seconds, 30),
        )
        ont_reconcile_seconds = resolve_integer(
            session, SettingDomain.network_monitoring, "ont_reconcile_interval_seconds"
        )
        _sync_scheduled_task(
            session,
            name="ont_reconcile_sweep",
            task_name="app.tasks.ont_reconcile.run_ont_reconcile_sweep",
            enabled=control_registry.is_enabled(session, "network.ont_reconcile"),
            interval_seconds=max(ont_reconcile_seconds, 300),
        )
        # Independent of network.ont_reconcile ON PURPOSE: the fleet-wide
        # control is often disabled while holds are in force, and gating this
        # on it would silence the overdue alarm exactly when it matters.
        _sync_scheduled_task(
            session,
            name="ont_reconcile_holds_overdue",
            task_name="app.tasks.ont_reconcile.alert_overdue_reconcile_holds",
            enabled=True,
            interval_seconds=3600,
        )
        ont_status_seconds = resolve_integer(
            session,
            SettingDomain.network_monitoring,
            "huawei_ont_status_interval_seconds",
        )
        _sync_scheduled_task(
            session,
            name="huawei_ont_status",
            task_name="app.tasks.ont_runtime_status.dispatch_huawei_ont_status",
            enabled=True,
            interval_seconds=max(ont_status_seconds, 120),
        )
        # RADIUS health: accounting freshness + enforcement drift from the
        # external radacct DB and the reconciled live-session view. Customer
        # experience often fails here before any router goes down.
        radius_health_seconds = resolve_integer(
            session, SettingDomain.network_monitoring, "radius_health_interval_seconds"
        )
        _sync_scheduled_task(
            session,
            name="radius_health_check",
            task_name="app.tasks.radius_health.run_radius_health_check",
            enabled=True,
            interval_seconds=max(radius_health_seconds, 60),
        )
        # Customer-impact counters (customers under outage / with infra
        # tickets / suppressed from billing comms) -> VictoriaMetrics trends.
        customer_impact_seconds = resolve_integer(
            session,
            SettingDomain.network_monitoring,
            "customer_impact_metrics_interval_seconds",
        )
        _sync_scheduled_task(
            session,
            name="customer_impact_metrics",
            task_name="app.tasks.customer_impact_metrics.export_customer_impact_metrics",
            enabled=True,
            interval_seconds=max(customer_impact_seconds, 120),
        )
        # Warm cached live status for topology nodes (read by the Network Path
        # panel). Default 180s, matching the monitoring dashboard cache TTL.
        topology_status_seconds = resolve_integer(
            session,
            SettingDomain.network_monitoring,
            "topology_status_warm_interval_seconds",
        )
        _sync_scheduled_task(
            session,
            name="topology_status_warm",
            task_name="app.tasks.topology_sync.warm_topology_status",
            enabled=True,
            interval_seconds=max(topology_status_seconds, 60),
        )
        # LLDP neighbor poll -> directed device graph. Physical adjacency changes
        # rarely; hourly is ample.
        lldp_poll_minutes = resolve_integer(
            session,
            SettingDomain.network_monitoring,
            "topology_lldp_poll_interval_minutes",
        )
        _sync_scheduled_task(
            session,
            name="topology_lldp_poll",
            task_name="app.tasks.topology_lldp.run_lldp_topology_poll",
            enabled=True,
            interval_seconds=max(lldp_poll_minutes * 60, 300),
        )
        # Reviewed forwarding declarations scope a read-only RouterOS BGP/route
        # poll. The fail-closed control keeps deployment from silently starting
        # collection before operators approve the observation shadow run.
        forwarding_observation_seconds = resolve_integer(
            session,
            SettingDomain.network_monitoring,
            "forwarding_control_observation_interval_seconds",
        )
        _sync_scheduled_task(
            session,
            name="forwarding_control_observation_poll",
            task_name=(
                "app.tasks.forwarding_control_observations."
                "run_forwarding_control_observation_poll"
            ),
            enabled=control_registry.is_enabled(
                session, "network.forwarding_observation_collection"
            ),
            interval_seconds=max(forwarding_observation_seconds, 60),
        )
        # The open-only auto-detect scan is retired: the classifier reconcile
        # below owns all auto detection (dark nodes + wireless clusters).
        _retire_scheduled_task(
            session,
            "app.tasks.topology_outage.run_outage_scan",
        )
        # Detected-outage incident reconcile (design ): debounce the
        # classifier's point-in-time verdicts into the suspected/confirmed/
        # clearing/resolved lifecycle. Aligned with the topology status warmer's
        # cadence so each pass reads freshly-warmed live_status; floor 120s.
        outage_reconcile_seconds = resolve_integer(
            session,
            SettingDomain.network_monitoring,
            "outage_reconcile_interval_seconds",
        )
        _sync_scheduled_task(
            session,
            name="topology_outage_reconcile",
            task_name="app.tasks.topology_outage.reconcile_detected_outages",
            enabled=True,
            interval_seconds=max(outage_reconcile_seconds, 120),
        )
        # Automated customer outage notification (ADR 0004). Scheduling this is
        # safe while the decision to enable it is still open: the service is
        # gated on OUTAGE_AUTO_NOTIFY_ENABLED (off) and defaults to dry-run, so
        # an enabled beat entry is a no-op until both are flipped. Cadence is
        # deliberately slower than the reconcile above — the settle window, not
        # the poll rate, decides how fast a customer hears.
        outage_auto_notify_seconds = resolve_integer(
            session,
            SettingDomain.network_monitoring,
            "outage_auto_notify_interval_seconds",
        )
        # The beat entry follows the feature flag rather than being permanently
        # scheduled: with automation off (the default) nothing is dispatched at
        # all, instead of a task firing every few minutes only to no-op. Enable
        # outage_auto_notify_enabled in System > Config > Automated Outage
        # Notification and the runner starts on the next schedule rebuild.
        outage_auto_notify_enabled = resolve_boolean(
            session,
            SettingDomain.network_monitoring,
            "outage_auto_notify_enabled",
        )
        _sync_scheduled_task(
            session,
            name="outage_auto_notify",
            task_name=(
                "app.tasks.outage_auto_notify.auto_dispatch_outage_notifications"
            ),
            enabled=outage_auto_notify_enabled,
            interval_seconds=max(outage_auto_notify_seconds, 120),
        )
        # UISP topology sync: import the wireless/UFiber customer-device
        # relationship layer (radios -> APs, ONUs -> UF-OLTs) into sub's
        # tables. Association churn is faster than the device graph, so it
        # runs on a 15-minute default cadence.
        topology_uisp_minutes = resolve_integer(
            session,
            SettingDomain.network_monitoring,
            "topology_uisp_sync_interval_minutes",
        )
        _sync_scheduled_task(
            session,
            name="topology_uisp_sync",
            task_name="app.tasks.topology_uisp.run_uisp_topology_sync",
            enabled=True,
            interval_seconds=max(topology_uisp_minutes * 60, 300),
        )
        _sync_scheduled_task(
            session,
            name="uisp_config_readback",
            task_name="app.tasks.uisp_control.reconcile_uisp_config_readback",
            enabled=True,
            interval_seconds=max(topology_uisp_minutes * 60, 300),
        )
        _sync_scheduled_task(
            session,
            name="router_config_readback",
            task_name="router_sync.reconcile_config_push_readback",
            enabled=True,
            interval_seconds=300,
        )
        _sync_scheduled_task(
            session,
            name="router_sot_drift",
            task_name="router_sync.audit_sot_drift",
            enabled=True,
            interval_seconds=900,
        )
        _sync_scheduled_task(
            session,
            name="mikrotik_nas_vlan_readback",
            task_name="router_sync.reconcile_nas_vlan_readback",
            enabled=True,
            interval_seconds=300,
        )
        # Preview-only UFiber ONU/subscription candidate audit. Router-mode
        # UF-Wifi ONUs carry an observed MAC from UISP, which can be compared to
        # ACTIVE-subscription MAC observations for field review. The task writes
        # neither customer assignments nor subscription MACs. It runs after the
        # UISP sync, so an hourly default (vs the 15-min importer) is ample.
        ufiber_onu_link_minutes = resolve_integer(
            session,
            SettingDomain.network_monitoring,
            "ufiber_onu_link_interval_minutes",
        )
        _sync_scheduled_task(
            session,
            name="ufiber_onu_link",
            task_name="app.tasks.topology_ufiber_link.run_ufiber_onu_link",
            enabled=True,
            interval_seconds=max(ufiber_onu_link_minutes * 60, 300),
        )
        # Topology coverage + pipeline-health metrics export to
        # VictoriaMetrics (per-medium E2E match-rate, feeder-task staleness).
        # Default 15 minutes — matches the fastest feeder (topology_uisp_sync),
        # so every feeder run is reflected without redundant recomputation.
        topology_metrics_seconds = resolve_integer(
            session,
            SettingDomain.network_monitoring,
            "topology_metrics_interval_seconds",
        )
        _sync_scheduled_task(
            session,
            name="topology_metrics_export",
            task_name="app.tasks.topology_metrics.export_topology_metrics",
            enabled=True,
            interval_seconds=max(topology_metrics_seconds, 300),
        )
        network_operation_metrics_seconds = resolve_integer(
            session,
            SettingDomain.network_monitoring,
            "network_operation_metrics_interval_seconds",
        )
        _sync_scheduled_task(
            session,
            name="network_operation_metrics",
            task_name="app.tasks.network_operations.publish_operation_metrics",
            enabled=True,
            interval_seconds=max(network_operation_metrics_seconds, 60),
        )
        network_operation_dispatch_seconds = resolve_integer(
            session,
            SettingDomain.network_monitoring,
            "network_operation_dispatch_interval_seconds",
        )
        _sync_scheduled_task(
            session,
            name="network_operation_dispatch",
            task_name=(
                "app.tasks.network_operation_dispatch."
                "publish_network_operation_dispatches"
            ),
            enabled=True,
            interval_seconds=max(network_operation_dispatch_seconds, 5),
        )
        _sync_scheduled_task(
            session,
            name="ont_commissioning_reconciliation",
            task_name="app.tasks.ont_commissioning.reconcile_intents",
            enabled=True,
            interval_seconds=60,
        )
        # The redis dashboard stats-summary path was removed (the overview
        # renders from the in-process global context); retire its beat row.
        _retire_scheduled_task(
            session, "app.tasks.app_cache.refresh_dashboard_stats_cache"
        )
        # Zabbix ONT snapshot / monitoring summary cache warmers retired with
        # the native monitoring cutover; disable any existing DB rows.
        _retire_scheduled_task(
            session, "app.tasks.app_cache.refresh_ont_zabbix_snapshot_cache"
        )
        _retire_scheduled_task(
            session, "app.tasks.monitoring_warm.warm_monitoring_caches"
        )
        _retire_scheduled_task(
            session,
            "app.tasks.ont_discovery.discover_all_olt_onts",
        )
        # Periodic SSH autofind polling has been replaced by syslog-based discovery.
        # Retire any existing scheduled task to disable it.
        _retire_scheduled_task(
            session,
            "app.tasks.ont_autofind.discover_all_olt_autofind",
        )

        # OLT config backup (SSH-based running config retrieval)
        olt_backup_hours = resolve_integer(
            session, SettingDomain.network_monitoring, "olt_backup_interval_hours"
        )
        _sync_scheduled_task(
            session,
            name="olt_config_backup",
            task_name="app.tasks.olt_config_backup.backup_all_olts",
            enabled=True,
            interval_seconds=max(olt_backup_hours * 3600, 3600),
        )
        # Router config backup (REST /export snapshots). Mirrors OLT backup —
        # the keystone for DR/rollback/change-history and for reading firewall/
        # CoA posture from stored config. The task self-gates to online routers.
        router_backup_hours = resolve_integer(
            session,
            SettingDomain.network_monitoring,
            "router_config_backup_interval_hours",
        )
        _sync_scheduled_task(
            session,
            name="router_config_backup",
            task_name="router_sync.capture_scheduled_snapshots",
            enabled=True,
            interval_seconds=max(router_backup_hours * 3600, 3600),
        )
        # NAS config backup orchestrator. The task itself honors each device's
        # backup_enabled + backup_schedule, so this just needs to run often
        # enough to catch due devices (hourly).
        nas_backup_interval = resolve_integer(
            session,
            SettingDomain.network_monitoring,
            "nas_config_backup_interval_seconds",
        )
        _sync_scheduled_task(
            session,
            name="nas_config_backup",
            task_name="app.tasks.nas.run_scheduled_backups",
            enabled=True,
            interval_seconds=max(nas_backup_interval, 900),
        )
        # Self-serve quote mirror reconcile — backstop for missed quote.* webhooks.
        quote_reconcile_seconds = resolve_integer(
            session, SettingDomain.subscriber, "quote_reconcile_interval_seconds"
        )
        _sync_scheduled_task(
            session,
            name="quote_mirror_reconcile",
            task_name="app.tasks.quotes.reconcile_quote_mirror",
            enabled=True,
            interval_seconds=max(quote_reconcile_seconds, 900),
        )
        olt_profile_sync_enabled = control_registry.is_enabled(
            session, "network.olt_profile_sync"
        )
        olt_profile_sync_interval_seconds = resolve_integer(
            session, SettingDomain.network, "olt_profile_sync_interval_seconds"
        )
        _sync_scheduled_task(
            session,
            name="olt_profile_sync_due_task_runner",
            task_name="app.tasks.profile_sync.execute_due_profile_sync_tasks",
            enabled=olt_profile_sync_enabled,
            interval_seconds=max(olt_profile_sync_interval_seconds, 60),
        )

        for removed_task_name in (
            "app.tasks.olt_capture.capture_olt_samples_task",
            "app.tasks.olt_capture.validate_all_parsers_task",
            "app.tasks.olt_capture.capture_all_olts_task",
            "app.tasks.provisioning_enforcement.run_enforcement",
        ):
            _retire_scheduled_task(session, removed_task_name)

        _retire_scheduled_task(session, "app.tasks.workflow.detect_sla_breaches")

        # WireGuard VPN maintenance tasks
        wg_log_cleanup_enabled = control_registry.is_enabled(session, "vpn.log_cleanup")
        wg_log_cleanup_interval = resolve_integer(
            session, SettingDomain.network, "wireguard_log_cleanup_interval_seconds"
        )
        wg_log_cleanup_interval = max(wg_log_cleanup_interval, 3600)  # Min: 1 hour
        _sync_scheduled_task(
            session,
            name="wireguard_log_cleanup",
            task_name="app.tasks.wireguard.cleanup_connection_logs",
            enabled=wg_log_cleanup_enabled,
            interval_seconds=wg_log_cleanup_interval,
        )

        # WireGuard token cleanup - runs hourly
        wg_token_cleanup_enabled = control_registry.is_enabled(
            session, "vpn.token_cleanup"
        )
        wg_token_cleanup_interval = resolve_integer(
            session, SettingDomain.network, "wireguard_token_cleanup_interval_seconds"
        )
        wg_token_cleanup_interval = max(
            wg_token_cleanup_interval, 300
        )  # Min: 5 minutes
        _sync_scheduled_task(
            session,
            name="wireguard_token_cleanup",
            task_name="app.tasks.wireguard.cleanup_expired_tokens",
            enabled=wg_token_cleanup_enabled,
            interval_seconds=wg_token_cleanup_interval,
        )

        _retire_scheduled_task(session, "app.tasks.wireguard.sync_peer_stats")

        # TR-069 ACS device sync - syncs devices from GenieACS
        tr069_sync_enabled = control_registry.is_enabled(session, "network.tr069_sync")
        tr069_sync_interval = resolve_integer(
            session, SettingDomain.network, "tr069_sync_interval_seconds"
        )
        tr069_sync_interval = max(tr069_sync_interval, 300)  # Min: 5 minutes
        _sync_scheduled_task(
            session,
            name="tr069_device_sync",
            task_name="app.tasks.tr069.sync_all_acs_devices",
            enabled=tr069_sync_enabled,
            interval_seconds=tr069_sync_interval,
        )

        # Permanent lifecycle drainage: adopted commands must reach a terminal
        # state even when new command admission is disabled.
        tr069_jobs_interval = resolve_integer(
            session,
            SettingDomain.network,
            "tr069_command_reconciliation_interval_seconds",
        )
        tr069_jobs_interval = max(tr069_jobs_interval, 30)  # Min: 30 seconds
        _sync_scheduled_task(
            session,
            name="tr069_command_reconciler",
            task_name="app.tasks.tr069.reconcile_command_outcomes",
            enabled=True,
            interval_seconds=tr069_jobs_interval,
        )

        # TR-069 device health check - monitors last_inform freshness
        tr069_health_enabled = control_registry.is_enabled(
            session, "network.tr069_health_check"
        )
        tr069_health_interval = resolve_integer(
            session, SettingDomain.network, "tr069_health_check_interval_seconds"
        )
        tr069_health_interval = max(tr069_health_interval, 300)  # Min: 5 minutes
        _sync_scheduled_task(
            session,
            name="tr069_health_checker",
            task_name="app.tasks.tr069.check_device_health",
            enabled=tr069_health_enabled,
            interval_seconds=tr069_health_interval,
        )

        # TR-069 record cleanup - deletes old sessions and jobs
        tr069_cleanup_enabled = control_registry.is_enabled(
            session, "network.tr069_cleanup"
        )
        tr069_cleanup_interval = resolve_integer(
            session, SettingDomain.network, "tr069_cleanup_interval_seconds"
        )
        tr069_cleanup_interval = max(tr069_cleanup_interval, 3600)  # Min: 1 hour
        _sync_scheduled_task(
            session,
            name="tr069_record_cleanup",
            task_name="app.tasks.tr069.cleanup_tr069_records",
            enabled=tr069_cleanup_enabled,
            interval_seconds=tr069_cleanup_interval,
        )

        # TR-069 GenieACS stale task cleanup - deletes stuck tasks/faults older than threshold
        # This prevents inform blocking loops from permanently failing tasks
        tr069_genieacs_cleanup_enabled = control_registry.is_enabled(
            session, "network.tr069_genieacs_stale_cleanup"
        )
        tr069_genieacs_cleanup_interval = resolve_integer(
            session,
            SettingDomain.network,
            "tr069_genieacs_stale_cleanup_interval_seconds",
        )
        tr069_genieacs_cleanup_interval = max(
            tr069_genieacs_cleanup_interval, 300
        )  # Min: 5 minutes
        _sync_scheduled_task(
            session,
            name="tr069_genieacs_stale_cleanup",
            task_name="app.tasks.tr069.cleanup_stale_genieacs_tasks",
            enabled=tr069_genieacs_cleanup_enabled,
            interval_seconds=tr069_genieacs_cleanup_interval,
        )

        # TR-069 GenieACS metrics scrape - pushes pending/faults/inform-age to VictoriaMetrics
        tr069_metrics_enabled = control_registry.is_enabled(
            session, "network.tr069_metrics_scrape"
        )
        tr069_metrics_interval = resolve_integer(
            session, SettingDomain.network, "tr069_metrics_scrape_interval_seconds"
        )
        tr069_metrics_interval = max(tr069_metrics_interval, 60)  # Min: 1 minute
        _sync_scheduled_task(
            session,
            name="tr069_metrics_scrape",
            task_name="app.tasks.tr069.scrape_genieacs_metrics",
            enabled=tr069_metrics_enabled,
            interval_seconds=tr069_metrics_interval,
        )

        # TR-069 ONT runtime refresh - updates observed WAN/WiFi/LAN data
        tr069_runtime_enabled = control_registry.is_enabled(
            session, "network.tr069_ont_runtime_refresh"
        )
        tr069_runtime_interval = resolve_integer(
            session, SettingDomain.network, "tr069_ont_runtime_refresh_interval_seconds"
        )
        tr069_runtime_interval = max(tr069_runtime_interval, 300)  # Min: 5 minutes
        _sync_scheduled_task(
            session,
            name="tr069_ont_runtime_refresh",
            task_name="app.tasks.tr069.refresh_ont_runtime_data",
            enabled=tr069_runtime_enabled,
            interval_seconds=tr069_runtime_interval,
        )

        # Authorization only registers the ONT. Online-silent ACS repair remains
        # a manual/operator recovery task only; do not run it as a periodic
        # provisioning loop.
        _retire_scheduled_task(session, "app.tasks.tr069.heal_online_silent_onts")
        _retire_scheduled_task(
            session,
            "app.tasks.ont_verification.verify_ont_provisioning_state",
        )

        # Durable event outbox recovery. The normal path dispatches immediately
        # after commit; this runner claims rows left pending by a process crash.
        event_dispatch_interval = resolve_integer(
            session, SettingDomain.scheduler, "event_dispatch_interval_seconds"
        )
        # Due-timer dispatch — permanent lifecycle infrastructure (ADR 0007
        # invariant 23). Cadence is configurable; the authority path is not.
        timer_dispatch_interval = resolve_integer(
            session, SettingDomain.scheduler, "durable_timer_dispatch_interval_seconds"
        )
        timer_dispatch_interval = max(timer_dispatch_interval, 30)
        _sync_scheduled_task(
            session,
            name="durable_timer_dispatch_runner",
            task_name="app.tasks.durable_timers.fire_due_durable_timers",
            enabled=True,
            interval_seconds=timer_dispatch_interval,
        )

        _sync_scheduled_task(
            session,
            name="event_dispatch_runner",
            task_name="app.tasks.events.dispatch_pending_events",
            enabled=True,
            interval_seconds=event_dispatch_interval,
        )

        # Event retry - retries failed event handlers
        event_retry_interval = resolve_integer(
            session, SettingDomain.scheduler, "event_retry_interval_seconds"
        )
        event_retry_interval = max(event_retry_interval, 60)  # Min: 1 minute
        _sync_scheduled_task(
            session,
            name="event_retry_runner",
            task_name="app.tasks.events.retry_failed_events",
            enabled=True,
            interval_seconds=event_retry_interval,
        )

        # Event stale processing cleanup - marks stuck events as failed
        event_stale_cleanup_interval = resolve_integer(
            session, SettingDomain.scheduler, "event_stale_cleanup_interval_seconds"
        )
        event_stale_cleanup_interval = max(
            event_stale_cleanup_interval, 60
        )  # Min: 1 minute
        _sync_scheduled_task(
            session,
            name="event_stale_cleanup_runner",
            task_name="app.tasks.events.mark_stale_processing_events",
            enabled=True,
            interval_seconds=event_stale_cleanup_interval,
        )

        stale_infra_check_enabled = _scheduler_setting_enabled(
            session,
            SettingDomain.scheduler,
            "stale_infrastructure_check_enabled",
        )
        stale_infra_check_interval = resolve_integer(
            session,
            SettingDomain.scheduler,
            "stale_infrastructure_check_interval_seconds",
        )
        stale_infra_check_interval = max(stale_infra_check_interval, 60)
        _sync_scheduled_task(
            session,
            name="stale_infrastructure_check",
            task_name="app.tasks.monitoring_cleanup.check_stale_infrastructure",
            enabled=stale_infra_check_enabled,
            interval_seconds=stale_infra_check_interval,
        )

        monitoring_inventory_sync_interval = resolve_integer(
            session,
            SettingDomain.network_monitoring,
            "monitoring_inventory_sync_interval_seconds",
        )
        _sync_scheduled_task(
            session,
            name="monitoring_inventory_sync",
            task_name="app.tasks.monitoring_cleanup.sync_inventory_to_monitoring",
            enabled=True,
            interval_seconds=max(monitoring_inventory_sync_interval, 300),
        )

        # Device-metric retention: the task existed but was never scheduled,
        # so device_metrics grew unbounded — and the native infrastructure
        # poller now writes a row per ping probe, making it the table's
        # biggest writer. Postgres keeps current state; history belongs to
        # VictoriaMetrics (see docs/designs/OPERATIONS_MEASUREMENT_STRATEGY).
        device_metrics_cleanup_hours = resolve_integer(
            session,
            SettingDomain.network_monitoring,
            "device_metrics_cleanup_interval_hours",
        )
        _sync_scheduled_task(
            session,
            name="device_metrics_cleanup",
            task_name="app.tasks.monitoring_cleanup.cleanup_old_device_metrics",
            enabled=True,
            interval_seconds=max(device_metrics_cleanup_hours, 1) * 3600,
        )

        # Event old cleanup - removes old completed events
        event_old_cleanup_enabled = _scheduler_setting_enabled(
            session,
            SettingDomain.scheduler,
            "event_old_cleanup_enabled",
        )
        event_old_cleanup_interval = resolve_integer(
            session, SettingDomain.scheduler, "event_old_cleanup_interval_seconds"
        )
        event_old_cleanup_interval = max(
            event_old_cleanup_interval, 3600
        )  # Min: 1 hour
        _sync_scheduled_task(
            session,
            name="event_old_cleanup_runner",
            task_name="app.tasks.events.cleanup_old_events",
            enabled=event_old_cleanup_enabled,
            interval_seconds=event_old_cleanup_interval,
        )

        # RADIUS sync - syncs NAS devices and users to FreeRADIUS
        radius_sync_enabled = _scheduler_setting_enabled(
            session,
            SettingDomain.radius,
            "radius_sync_enabled",
        )
        radius_sync_interval = resolve_integer(
            session, SettingDomain.radius, "radius_sync_interval_seconds"
        )
        radius_sync_interval = max(radius_sync_interval, 60)  # Min: 1 minute
        if radius_sync_enabled:
            # Get all active sync jobs and schedule them
            from app.models.radius import RadiusSyncJob

            sync_jobs = (
                session.query(RadiusSyncJob)
                .filter(RadiusSyncJob.is_active.is_(True))
                .all()
            )
            for sync_job in sync_jobs:
                schedule[f"radius_sync_{sync_job.id}"] = {
                    "task": "app.tasks.radius.run_radius_sync_job",
                    "schedule": timedelta(seconds=radius_sync_interval),
                    "args": [str(sync_job.id)],
                }

        # CRM ticket pull: inbound CRM tickets/comments into local support tickets.
        crm_ticket_pull_enabled = control_registry.is_enabled(
            session, "crm.ticket_pull"
        )
        crm_ticket_pull_interval = resolve_integer(
            session, SettingDomain.scheduler, "crm_ticket_pull_interval_minutes"
        )
        crm_ticket_pull_interval = max(crm_ticket_pull_interval, 1)
        from app.services.integrations.crm_ticket_readiness import (
            resolve_crm_ticket_pull_readiness,
        )

        crm_ticket_readiness = resolve_crm_ticket_pull_readiness(
            session,
            control_enabled=crm_ticket_pull_enabled,
        )
        if crm_ticket_readiness.schedule_enabled:
            schedule["crm_ticket_pull"] = {
                "task": "app.tasks.crm_ticket_pull.pull_crm_tickets",
                "schedule": timedelta(minutes=crm_ticket_pull_interval),
            }
            # Daily full reconciliation: heals drift the incremental runs
            # can't see (CRM comments don't bump ticket updated_at; closed
            # tickets are excluded from the incremental comment sweep).
            schedule["crm_ticket_pull_full"] = {
                "task": "app.tasks.crm_ticket_pull.pull_crm_tickets",
                "schedule": crontab(hour=3, minute=40),
                "kwargs": {"full": True},
            }
        elif crm_ticket_pull_enabled:
            logger.error(
                "crm_ticket_pull_not_ready issue_codes=%s",
                ",".join(crm_ticket_readiness.issue_codes),
            )

        # ERP schedules derive from validated capability bindings. Per-flow
        # single-writer ownership remains the independent business cutover gate.
        from app.services.integrations.backoffice_contracts import (
            ERP_INVENTORY_CAPABILITY,
            ERP_OPERATIONAL_SYNC_CAPABILITY,
            ERP_OUTBOX_CAPABILITY,
            ERP_STATUS_CAPABILITY,
        )
        from app.services.integrations.erp_capability import capability_enabled

        erp_outbox_enabled = capability_enabled(session, ERP_OUTBOX_CAPABILITY)
        erp_inventory_enabled = capability_enabled(session, ERP_INVENTORY_CAPABILITY)
        erp_status_enabled = capability_enabled(session, ERP_STATUS_CAPABILITY)
        erp_operational_sync_enabled = capability_enabled(
            session, ERP_OPERATIONAL_SYNC_CAPABILITY
        )
        dotmac_erp_outbox_interval = resolve_integer(
            session, SettingDomain.integration, "dotmac_erp_outbox_interval_seconds"
        )
        dotmac_erp_outbox_interval = max(dotmac_erp_outbox_interval, 30)
        _sync_scheduled_task(
            session,
            name="dotmac_erp_material_catalog_refresh",
            task_name="app.tasks.dotmac_erp_outbox.refresh_material_catalog",
            enabled=erp_inventory_enabled,
            interval_seconds=86400,
        )
        _sync_scheduled_task(
            session,
            name="dotmac_erp_outbox_delivery",
            task_name="app.tasks.dotmac_erp_outbox.deliver_erp_sync_events",
            enabled=erp_outbox_enabled,
            interval_seconds=dotmac_erp_outbox_interval,
        )

        # Expense-claim status reconciliation. Polls ERP for
        # in-flight claims and refreshes expense_claim_status on the source row.
        # The validated ERP status capability gates this read-only schedule;
        # per-flow ownership independently gates outbound delivery.
        dotmac_erp_expense_refresh_interval = resolve_integer(
            session,
            SettingDomain.integration,
            "dotmac_erp_expense_status_refresh_interval_seconds",
        )
        _sync_scheduled_task(
            session,
            name="dotmac_erp_expense_status_refresh",
            task_name="app.tasks.dotmac_erp_outbox.refresh_expense_claim_statuses",
            enabled=erp_status_enabled,
            interval_seconds=max(dotmac_erp_expense_refresh_interval, 120),
        )

        # Material-request status reconciliation. Polls ERP for
        # in-flight ISSUE requests and refreshes support_status on the source
        # row (flipping to fulfilled when ERP reports it). The validated ERP
        # status capability gates this read-only schedule; per-flow ownership
        # independently gates outbound delivery.
        dotmac_erp_material_refresh_interval = resolve_integer(
            session,
            SettingDomain.integration,
            "dotmac_erp_material_status_refresh_interval_seconds",
        )
        _sync_scheduled_task(
            session,
            name="dotmac_erp_material_status_refresh",
            task_name="app.tasks.dotmac_erp_outbox.refresh_material_request_statuses",
            enabled=erp_status_enabled,
            interval_seconds=max(dotmac_erp_material_refresh_interval, 120),
        )

        # Purchase invoices may become eligible after their PO write-back and
        # attachment delivery is deliberately independent from AP creation.
        _sync_scheduled_task(
            session,
            name="dotmac_erp_purchase_invoice_repair",
            task_name="app.tasks.dotmac_erp_outbox.repair_purchase_invoice_sync",
            enabled=erp_outbox_enabled,
            interval_seconds=max(dotmac_erp_outbox_interval, 60),
        )

        # ERP remains authoritative for AP settlement. Poll its dedicated
        # source-invoice read contract and project timestamped observations for
        # the vendor portal; never infer payment from the creation response.
        dotmac_erp_purchase_invoice_status_interval = resolve_integer(
            session,
            SettingDomain.integration,
            "dotmac_erp_purchase_invoice_status_refresh_interval_seconds",
        )
        _sync_scheduled_task(
            session,
            name="dotmac_erp_purchase_invoice_status_refresh",
            task_name=("app.tasks.dotmac_erp_outbox.refresh_purchase_invoice_statuses"),
            enabled=erp_status_enabled,
            interval_seconds=max(dotmac_erp_purchase_invoice_status_interval, 120),
        )

        # Operational context has its own capability so money transport and
        # context projection can be enabled independently.
        _sync_scheduled_task(
            session,
            name="dotmac_erp_operational_domain_sync",
            task_name="app.tasks.dotmac_erp_outbox.sync_erp_operational_domains",
            enabled=erp_operational_sync_enabled,
            interval_seconds=300,
        )

        # NOTE: the OLT deferred-operations queue + SSH circuit-breaker
        # subsystem was removed (it was never wired — the queue had no
        # producers and the real write paths bypassed the breaker, so it gave
        # false confidence). OLT writes happen directly; reconciliation/cleanup
        # is handled by the ONT reconcile sweeper. The inert schema
        # (queued_olt_operations table + OLTDevice circuit_* columns) was
        # dropped in migration 162.

        # Zabbix device sync retired with the native monitoring cutover;
        # disable any existing DB rows (old copy + surviving copy).
        _retire_scheduled_task(
            session, "app.tasks.zabbix_ingestion.sync_devices_to_zabbix"
        )
        _retire_scheduled_task(session, "app.tasks.zabbix_sync.sync_devices_to_zabbix")
        _sync_scheduled_task(
            session,
            name="infrastructure_admin_alert_evaluation",
            task_name="app.tasks.admin_alerts.evaluate_infrastructure_alerts",
            enabled=True,
            interval_seconds=60,
        )

        # Per-channel inbound freshness + Celery queue depth for the
        # dead-man's-switch and queue alerts. Default-on: it is a read-only
        # observer and the freshness signal is only useful if it runs
        # continuously. Frequent by design so a stalled channel surfaces in
        # minutes, not hours.
        channel_health_interval_seconds = max(
            resolve_integer(
                session,
                SettingDomain.network_monitoring,
                "channel_health_interval_seconds",
            ),
            30,
        )
        _sync_scheduled_task(
            session,
            name="channel_health_observer",
            task_name="app.tasks.channel_health.observe_channel_health",
            enabled=True,
            interval_seconds=channel_health_interval_seconds,
        )

        # Weekly NCC complaints digest — default OFF. Monday 08:00 in the
        # configured celery timezone (Africa/Lagos in prod). The service
        # short-circuits when disabled or already sent today, so a missed
        # beat self-heals on the next fire without double-sending.
        ncc_report_email_enabled = _scheduler_setting_enabled(
            session,
            SettingDomain.notification,
            "ncc_report_email_enabled",
        )
        if ncc_report_email_enabled:
            schedule["ncc_report_email"] = {
                "task": "app.tasks.reports.send_scheduled_ncc_report",
                "schedule": crontab(hour=8, minute=0, day_of_week="mon"),
            }

        tasks = (
            session.query(ScheduledTask).filter(ScheduledTask.enabled.is_(True)).all()
        )
        for task in tasks:
            entry = _scheduled_row_to_entry(task)
            if entry is not None:
                schedule[entry[0]] = entry[1]
    except Exception:
        logger.exception("Failed to build Celery beat schedule.")
    finally:
        session.close()
    return schedule
