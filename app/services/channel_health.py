"""Channel and queue health observations (docs/designs/CHANNEL_OBSERVABILITY.md).

Produces the two worker-side snapshots that the dead-man's-switch and the
queue-depth alerts read: per-channel inbound freshness and per-queue depth.
Both are pure reads published through the shared observability snapshot bridge,
so the web process can export them without the worker's in-memory gauges (which
it never sees). Every read is fail-soft: a broker hiccup must not stop the
freshness snapshot, and neither snapshot may ever raise into the beat task.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Literal, TypedDict, cast

from sqlalchemy import and_, case, func
from sqlalchemy.orm import Session

from app.services import channel_health_contracts
from app.services.observability import StateObservation, publish_state_snapshot
from app.services.team_inbox_health import SMTP_PROBE_VERIFIED_KEY

logger = logging.getLogger(__name__)

ChannelHealthStatus = Literal["ok", "degraded", "error"]


class ChannelHealthPublishSummary(TypedDict):
    channels: int
    contract_status: ChannelHealthStatus
    queues: int
    queue_status: ChannelHealthStatus


CHANNEL_INGESTION_DOMAIN = "channel_ingestion"
CELERY_QUEUES_DOMAIN = "celery_queues"

# Window for the "is this channel still producing" volume signal. Kept short so
# the count reflects the live rate, not an hour-old average.
_FRESHNESS_WINDOW = timedelta(minutes=15)


def collect_channel_ingestion_observations(
    db: Session, *, now: datetime | None = None
) -> list[StateObservation]:
    """Publish raw facts and enforceable policy signals for every contract.

    Natural freshness excludes owner-verified synthetic rows. Historically
    active channels keep their raw facts even when disabled; enabled contracts
    without history receive an actionable policy age rather than disappearing.
    """
    from app.models.team_inbox import InboxMessage, InboxMessageDirection

    moment = now or datetime.now(UTC)
    window_start = moment - _FRESHNESS_WINDOW
    verified_probe = InboxMessage.metadata_[SMTP_PROBE_VERIFIED_KEY].as_boolean()
    is_verified_probe = verified_probe.is_(True)
    is_natural = verified_probe.is_not(True)
    latest_natural = func.max(case((is_natural, InboxMessage.received_at), else_=None))
    recent = func.sum(
        case(
            (
                and_(is_natural, InboxMessage.received_at >= window_start),
                1,
            ),
            else_=0,
        )
    )
    latest_probe = func.max(
        case((is_verified_probe, InboxMessage.received_at), else_=None)
    )
    rows = (
        db.query(
            InboxMessage.channel_type,
            latest_natural,
            recent,
            latest_probe,
        )
        .filter(
            InboxMessage.direction == InboxMessageDirection.inbound.value,
            InboxMessage.channel_type.in_(
                channel_health_contracts.SUPPORTED_EXTERNAL_CHANNELS
            ),
        )
        .group_by(InboxMessage.channel_type)
        .all()
    )

    channel_facts: dict[str, tuple[datetime | None, int, datetime | None]] = {}
    observations: list[StateObservation] = []
    for channel_type, last_received, recent_count, last_probe in rows:
        channel = str(channel_type or "unknown")
        channel_facts[channel] = (last_received, int(recent_count or 0), last_probe)
        if last_received is not None:
            if last_received.tzinfo is None:
                last_received = last_received.replace(tzinfo=UTC)
            age = max(0.0, (moment - last_received).total_seconds())
            observations.append(
                StateObservation("seconds_since_last_inbound", channel, age)
            )
        observations.append(
            StateObservation("inbound_count_15m", channel, float(recent_count or 0))
        )

    contracts = channel_health_contracts.load_channel_health_contracts(db)
    for contract in contracts:
        last_received, _recent_count, last_probe = channel_facts.get(
            contract.channel,
            (None, 0, None),
        )
        active, _window_elapsed = (
            channel_health_contracts.active_window_elapsed_seconds(
                contract,
                now=moment,
            )
        )
        monitoring_active = contract.enabled and active
        synthetic_limit = contract.synthetic_max_age_seconds or 0
        observations.extend(
            (
                StateObservation(
                    "contract_enabled", contract.channel, float(contract.enabled)
                ),
                StateObservation(
                    "monitoring_active", contract.channel, float(monitoring_active)
                ),
                StateObservation(
                    "natural_required",
                    contract.channel,
                    float(contract.requires_natural_traffic),
                ),
                StateObservation(
                    "synthetic_required",
                    contract.channel,
                    float(contract.requires_synthetic_probe),
                ),
                StateObservation(
                    "severity_critical",
                    contract.channel,
                    float(contract.severity == "critical"),
                ),
                StateObservation(
                    "max_quiet_seconds",
                    contract.channel,
                    float(contract.max_quiet_seconds),
                ),
                StateObservation(
                    "synthetic_max_age_seconds",
                    contract.channel,
                    float(synthetic_limit),
                ),
                StateObservation(
                    "silence_age_seconds",
                    contract.channel,
                    channel_health_contracts.effective_age_seconds(
                        contract,
                        observed_at=last_received,
                        now=moment,
                    ),
                ),
                StateObservation(
                    "synthetic_age_seconds",
                    contract.channel,
                    channel_health_contracts.effective_age_seconds(
                        contract,
                        observed_at=last_probe,
                        now=moment,
                        max_age_seconds=synthetic_limit or None,
                    ),
                ),
                StateObservation(
                    "history_present",
                    contract.channel,
                    float(last_received is not None),
                ),
                StateObservation(
                    "synthetic_history_present",
                    contract.channel,
                    float(last_probe is not None),
                ),
            )
        )
    return observations


def collect_celery_queue_observations() -> list[StateObservation]:
    """Depth of each configured Celery queue, read as a Redis LLEN.

    The queue set comes from the Celery config, never a literal list, so a new
    queue is covered the moment it is routed. Redis is the only broker Sub runs;
    on any connection error the caller downgrades the snapshot rather than
    failing, so an unreachable broker degrades the queue signal without taking
    the freshness signal down with it.
    """
    from app.celery_app import celery_app

    queue_names = [
        queue.name
        for queue in (celery_app.conf.task_queues or [])
        if getattr(queue, "name", None)
    ]
    if not queue_names:
        return []

    import redis

    client = redis.Redis.from_url(celery_app.conf.broker_url)
    try:
        # llen is typed as a sync/async union in redis-py's stubs; this is the
        # synchronous client, so the result is a plain int.
        return [
            StateObservation("queue_depth", name, float(cast(int, client.llen(name))))
            for name in queue_names
        ]
    finally:
        try:
            client.close()
        except Exception:
            pass


def _publish_snapshot_fail_soft(
    domain: str,
    observations: Sequence[StateObservation],
    *,
    status: ChannelHealthStatus,
    now: datetime,
) -> ChannelHealthStatus:
    try:
        publish_state_snapshot(domain, observations, status=status, now=now)
        return status
    except Exception:
        logger.exception(
            "channel_health_snapshot_publish_failed",
            extra={"domain": domain},
        )
        return "error"


def publish_channel_health(
    db: Session, *, now: datetime | None = None
) -> ChannelHealthPublishSummary:
    """Compute and publish both snapshots; returns a small summary for logging.

    The queue read can fail independently of the DB read, so the two snapshots
    carry their own status: ingestion is ``error`` when its authoritative
    contract registry is invalid, and the queue snapshot is ``degraded``
    (empty) when the broker is unreachable. Neither failure propagates.
    """
    moment = now or datetime.now(UTC)

    ingestion_status: ChannelHealthStatus = "ok"
    try:
        ingestion = collect_channel_ingestion_observations(db, now=moment)
    except channel_health_contracts.ChannelHealthContractError:
        logger.exception("channel_health_contract_registry_invalid")
        ingestion = []
        ingestion_status = "error"
    ingestion_status = _publish_snapshot_fail_soft(
        CHANNEL_INGESTION_DOMAIN,
        ingestion,
        status=ingestion_status,
        now=moment,
    )

    queue_status: ChannelHealthStatus = "ok"
    try:
        queues = collect_celery_queue_observations()
    except Exception:
        logger.warning("channel_health_queue_read_failed", exc_info=True)
        queues = []
        queue_status = "degraded"
    queue_status = _publish_snapshot_fail_soft(
        CELERY_QUEUES_DOMAIN, queues, status=queue_status, now=moment
    )

    return {
        "channels": len({obs.scope for obs in ingestion}),
        "contract_status": ingestion_status,
        "queues": len(queues),
        "queue_status": queue_status,
    }
