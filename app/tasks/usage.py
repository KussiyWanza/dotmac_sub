import logging
from uuid import uuid4

from app.celery_app import celery_app
from app.schemas.usage import UsageRatingRunRequest
from app.services import fup_enforcement
from app.services import usage as usage_service
from app.services.db_session_adapter import db_session_adapter

logger = logging.getLogger(__name__)
SessionLocal = db_session_adapter.create_session


@celery_app.task(name="app.tasks.usage.run_usage_rating")
def run_usage_rating():
    with db_session_adapter.session() as session:
        usage_service.usage_rating_runs.run(session, UsageRatingRunRequest())


@celery_app.task(name="app.tasks.usage.import_radius_accounting")
def import_radius_accounting():
    from app.tasks._postgres_lock import postgres_session_advisory_lock

    with postgres_session_advisory_lock(_RADIUS_ACCOUNTING_IMPORT_LOCK_KEY) as acquired:
        if not acquired:
            logger.info("RADIUS accounting import skipped: another run is active")
            return {
                "ok": True,
                "processed": 0,
                "created_or_updated": 0,
                "cursor": None,
                "skipped_locked": 1,
            }
        return _import_radius_accounting_locked()


def _import_radius_accounting_locked():
    with db_session_adapter.session() as session:
        result = usage_service.import_radius_accounting(session)
        if result.get("ok") is not True:
            source_status = str(result.get("source_status") or "unavailable")
            raise RuntimeError(f"RADIUS accounting source is {source_status}")
        return result


@celery_app.task(name="app.tasks.usage.reap_stale_radius_sessions")
def reap_stale_radius_sessions():
    """Close accounting sessions whose feed went silent (NAS reboot / lost
    Stop packet) so they stop rendering as "active" forever. Safe only
    because the importer's refresh pass keeps last_update_at fresh for
    genuinely live sessions."""
    with db_session_adapter.session() as session:
        result = usage_service.reap_stale_radius_sessions(session)
        return result


@celery_app.task(name="app.tasks.usage.notify_expiring_data_bundles")
def notify_expiring_data_bundles():
    """Warn customers whose data bundles lapse within the next 24 hours.

    Runs daily, so each bundle falls inside the [now, now+24h) window exactly
    once — natural dedupe without extra state. Emits usage.addon_expiring,
    which the notification handler fans out to push + email."""
    from datetime import UTC, datetime, timedelta

    from app.models.catalog import AddOn, SubscriptionAddOn
    from app.services.events import emit_event
    from app.services.events.types import EventType

    with db_session_adapter.session() as session:
        now = datetime.now(UTC)
        window_end = now + timedelta(hours=24)
        rows = (
            session.query(SubscriptionAddOn, AddOn)
            .join(AddOn, AddOn.id == SubscriptionAddOn.add_on_id)
            .filter(AddOn.grant_gb.isnot(None))
            .filter(SubscriptionAddOn.end_at.isnot(None))
            .filter(SubscriptionAddOn.end_at >= now)
            .filter(SubscriptionAddOn.end_at < window_end)
            .all()
        )
        notified = 0
        for sub_add_on, add_on in rows:
            subscription = sub_add_on.subscription
            if subscription is None:
                continue
            grant = (add_on.grant_gb or 0) * int(sub_add_on.quantity or 1)
            emit_event(
                session,
                EventType.addon_expiring,
                {
                    "subscription_id": str(sub_add_on.subscription_id),
                    "account_id": str(subscription.subscriber_id),
                    "addon_name": add_on.name,
                    "grant_gb": str(grant),
                    "expires_at": sub_add_on.end_at.isoformat(),
                },
                subscription_id=sub_add_on.subscription_id,
                account_id=subscription.subscriber_id,
            )
            notified += 1
        return {"notified": notified}


@celery_app.task(name="app.tasks.usage.meter_usage_into_quota")
def meter_usage_into_quota():
    """Roll imported RADIUS accounting into the current period's quota buckets
    for capped subscriptions (the metering that feeds FUP/overage)."""
    with db_session_adapter.session() as session:
        result = usage_service.meter_usage_into_quota(session)
    changed_subscription_ids = result.get("changed_subscription_ids") or []
    if changed_subscription_ids:
        evaluate_fup_rules.apply_async(
            kwargs={
                "subscription_ids": changed_subscription_ids,
                "source": "usage_metering",
            },
            queue="billing",
        )
    return result


_FUP_EVALUATION_LOCK_KEY = 778_003
_RADIUS_ACCOUNTING_IMPORT_LOCK_KEY = 778_004


@celery_app.task(name="app.tasks.usage.evaluate_fup_rules")
def evaluate_fup_rules(
    subscription_ids: list[str] | None = None,
    source: str = "scheduled_full_sweep",
    contention_retry: bool = False,
) -> dict[str, int]:
    from sqlalchemy import func, select

    lock_db = SessionLocal()
    try:
        bind = lock_db.bind
        is_pg = bind is not None and bind.dialect.name == "postgresql"
        if is_pg:
            acquired = lock_db.execute(
                select(func.pg_try_advisory_lock(_FUP_EVALUATION_LOCK_KEY))
            ).scalar()
            # Commit immediately after taking the session-level advisory lock.
            # The lock survives commit, and the connection is no longer left
            # "idle in transaction" while the FUP sweep runs.
            lock_db.commit()
            if not acquired:
                logger.info("FUP evaluation skipped: another run is still active")
                return {
                    "processed": 0,
                    "enforced": 0,
                    "submonthly_no_data": 0,
                    "reset": 0,
                    "notified": 0,
                    "throttle_unconfigured": 0,
                    "targeted": int(subscription_ids is not None),
                    "skipped_locked": 1,
                }
        try:
            owner_db = SessionLocal()
            try:
                outcome = fup_enforcement.run_fup_evaluation(
                    owner_db,
                    fup_enforcement.RunFupSweepRequest(
                        correlation_id=uuid4(),
                        subscription_ids=(
                            tuple(subscription_ids)
                            if subscription_ids is not None
                            else None
                        ),
                        source=source,
                    ),
                )
            finally:
                owner_db.close()
            totals = outcome.totals
            result = {
                "processed": totals.processed,
                "enforced": totals.enforced,
                "reset": totals.reset,
                "notified": totals.notified,
                "submonthly_no_data": totals.submonthly_no_data,
                "throttle_unconfigured": totals.throttle_unconfigured,
                "targeted": int(subscription_ids is not None),
                "retried": outcome.retried,
                "deferred": len(outcome.deferred_subscription_ids),
                "deferred_retry_queued": 0,
                "retry_enqueue_failed": 0,
            }
            if outcome.deferred_subscription_ids and not contention_retry:
                try:
                    evaluate_fup_rules.apply_async(
                        kwargs={
                            "subscription_ids": [
                                str(value)
                                for value in outcome.deferred_subscription_ids
                            ],
                            "source": source,
                            "contention_retry": True,
                        },
                        queue="billing",
                        countdown=60,
                    )
                    result["deferred_retry_queued"] = len(
                        outcome.deferred_subscription_ids
                    )
                except (
                    Exception
                ):  # Broker submission only; never replay committed work.
                    logger.exception(
                        "FUP contention retry enqueue failed; full sweep will revisit candidates"
                    )
                    result["retry_enqueue_failed"] = 1
            return result
        finally:
            if is_pg:
                lock_db.execute(
                    select(func.pg_advisory_unlock(_FUP_EVALUATION_LOCK_KEY))
                )
                lock_db.commit()
    finally:
        lock_db.close()


@celery_app.task(name="app.tasks.usage.lift_expired_fup_enforcement")
def lift_expired_fup_enforcement() -> dict[str, int]:
    """Queue-independent safety-net that lifts FUP enforcement past its reset.

    The primary reset is inline in ``evaluate_fup_rules`` (billing queue); if
    that queue stalls, throttled/blocked customers would never be auto-lifted
    after their consumption window ends. This standalone sweep reads
    ``list_pending_reset`` and lifts each state independently, so reset survives
    a billing-queue outage. Idempotent — an already-cleared state is a no-op.
    """
    owner_db = SessionLocal()
    try:
        return fup_enforcement.run_expired_fup_lift(
            owner_db,
            fup_enforcement.RunExpiredFupLiftRequest(correlation_id=uuid4()),
        )
    finally:
        owner_db.close()
