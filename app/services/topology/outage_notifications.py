"""Outage notification send-path — outage classifier P4 (design §P4).

Compose the customer-facing outage notifications and split them the way that
matters (design §5): an **area outage** gets ONE "known outage, we're on it"
message per affected customer, while an isolated **last-mile** fault gets that
customer's specific advice. Telling 200 customers on a cut splitter to "reboot
your router" is exactly the failure this split prevents.

**Channel selection is NOT ours.** This module decides *who* to notify and
*what* to say; it hands each notification to the notification system by emitting
an ``outage_area`` / ``outage_last_mile`` event (``emit_event``). That system
owns channels-per-type (registered in ``EVENT_NOTIFICATION_SPECS``, overridable
via ``notification_event_<type>_channels``), per-subscriber preferences, opt-out,
and delivery. Email is the configured default. For an AREA (fiber) outage the
customer's home link is down, so email may only arrive once they reconnect / on
cellular — that's fine for "we're on it / here's what happened"; it is NOT a
real-time alarm.

Safety: real dispatch requires ALL of — ``OUTAGE_NOTIFY_ENABLED`` on, an explicit
operator ``actor_id`` (there is NO Celery beat / auto-trigger that sends to real
customers), the boundary passing the confidence gate, the boundary not
debounced (persisted, cross-worker), and the per-run cap. Every attempt writes
an ``OutageNotificationDispatch`` audit row; that table is also the debounce
source. ``plan_outage_notifications`` is the read-only preview.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.config import settings
from app.models.catalog import Subscription
from app.models.network_monitoring import (
    NetworkDevice,
    OutageIncident,
    OutageNotificationDispatch,
)
from app.services.customer_notification_policy import (
    is_notification_enabled_for_subscriber,
)
from app.services.device_operational_status import warmer_is_stale
from app.services.notification_adapter import NotificationCategory, NotificationChannel
from app.services.topology import connection_status
from app.services.topology.affected import affected_customers
from app.services.topology.connection_status import STATE_CONNECTED, STATE_OUTAGE
from app.services.topology.health_classifier import NODE_OUTAGE, localize_outage
from app.services.topology.outage import (
    CLASSIFIER_CUSTOMER_VISIBLE_STATUSES,
    CLASSIFIER_SOURCE,
    OPERATOR_SOURCE,
)

logger = logging.getLogger(__name__)

# Category the outage notifications are filed under (for opt-out + routing). The
# notification system resolves the actual CHANNELS from the per-type registry —
# we never name a channel here. The coarse opt-out check below uses email (the
# configured default) purely to avoid emitting for an opted-out customer and to
# audit it; the notification system remains the authoritative opt-out enforcer.
OUTAGE_NOTIFY_CATEGORY = NotificationCategory.service

# Notification *types* we emit (registered in EVENT_NOTIFICATION_SPECS as the
# EventType members below). Stored in the audit ``channel`` column, since the
# concrete channels are the notification system's config-driven concern.
_TYPE_AREA = "outage_area"
_TYPE_LAST_MILE = "outage_last_mile"

# Audit statuses.
_SENT = "sent"  # emitted to the notification system (which owns delivery)
_FAILED = "failed"
_SUPPRESSED_OPTOUT = "suppressed_optout"
_SKIPPED_DEBOUNCE = "skipped_debounce"
_SKIPPED_LOW_CONFIDENCE = "skipped_low_confidence"
_SKIPPED_CAP = "skipped_cap"
_SKIPPED_NO_RECIPIENT = "skipped_no_recipient"
_SKIPPED_INVALID_INCIDENT = "skipped_invalid_incident"


def _enabled() -> bool:
    return bool(getattr(settings, "outage_notify_enabled", False))


def _debounce_window() -> timedelta:
    return timedelta(hours=int(getattr(settings, "outage_notify_debounce_hours", 6)))


def _max_per_run() -> int:
    return int(getattr(settings, "outage_notify_max_per_run", 500))


def _batch_size() -> int:
    return max(1, int(getattr(settings, "outage_notify_batch_size", 50)))


def _area_min_affected() -> int:
    return int(getattr(settings, "outage_notify_area_min_affected", 5))


@dataclass
class _Target:
    subscription_id: uuid.UUID
    subscriber_id: uuid.UUID | None
    email: str | None
    subscriber_name: str
    opted_in: bool
    message: str


def _compose_message(a) -> str:
    """Customer-safe body from an assessment. Area messages already have the
    last-mile blame suppressed (advice is None); last-mile keeps its advice."""
    parts = [a.message]
    if a.advice:
        parts.append(a.advice)
    return "\n\n".join(p for p in parts if p)


def _collect(
    session: Session, subscription_ids, now: datetime
) -> tuple[dict[uuid.UUID, list[_Target]], dict[str, list[_Target]]]:
    """Group troubled customers into area (by boundary) and per-customer (by
    verdict) buckets. Connected customers are dropped."""
    ids = list(subscription_ids)
    subs = (
        session.query(Subscription).filter(Subscription.id.in_(ids)).all()
        if ids
        else []
    )
    area: dict[uuid.UUID, list[_Target]] = {}
    per_customer: dict[str, list[_Target]] = {}
    for sub in subs:
        a = connection_status.assess(session, sub, now=now)
        if a.state == STATE_CONNECTED:
            continue
        subscriber = sub.subscriber
        email = getattr(subscriber, "email", None)
        name = (
            f"{getattr(subscriber, 'first_name', '') or ''} "
            f"{getattr(subscriber, 'last_name', '') or ''}".strip()
            if subscriber is not None
            else ""
        ) or "Customer"
        opted_in = is_notification_enabled_for_subscriber(
            session,
            subscriber_id=sub.subscriber_id,
            # Coarse category-level gate on the default channel; the notification
            # system does the authoritative per-channel opt-out at send time.
            channel=NotificationChannel.email.value,
            category=OUTAGE_NOTIFY_CATEGORY.value,
            recipient=email,
        )
        target = _Target(
            subscription_id=sub.id,
            subscriber_id=sub.subscriber_id,
            email=email,
            subscriber_name=name,
            opted_in=opted_in,
            message=_compose_message(a),
        )
        if a.state == STATE_OUTAGE and a.area_boundary_id is not None:
            area.setdefault(a.area_boundary_id, []).append(target)
        else:
            per_customer.setdefault(a.verdict, []).append(target)
    return area, per_customer


def _boundary_debounced(
    session: Session, boundary_id: uuid.UUID, now: datetime
) -> bool:
    """True if this boundary was already notified within the debounce window —
    persisted, so it holds across workers and restarts (design §7.6/§7.7)."""
    cutoff = now - _debounce_window()
    last = (
        session.query(func.max(OutageNotificationDispatch.created_at))
        .filter(
            OutageNotificationDispatch.boundary_node_id == boundary_id,
            OutageNotificationDispatch.status == _SENT,
            OutageNotificationDispatch.created_at >= cutoff,
        )
        .scalar()
    )
    return last is not None


def _dedup_debounced(session: Session, dedup_key: str, now: datetime) -> bool:
    cutoff = now - _debounce_window()
    row = (
        session.query(OutageNotificationDispatch.id)
        .filter(
            OutageNotificationDispatch.dedup_key == dedup_key,
            OutageNotificationDispatch.status == _SENT,
            OutageNotificationDispatch.created_at >= cutoff,
        )
        .first()
    )
    return row is not None


def _area_boundary_qualifies(
    session: Session, boundary_id: uuid.UUID, now: datetime
) -> bool:
    """Confidence gate for a real area send (design §3).

    Trusted if the boundary is an OPERATOR-declared open incident or a debounced
    classifier incident (``confirmed``/``clearing``). Otherwise it must be an
    INFERRED, localized ``node_outage`` boundary with **high** confidence.
    """
    incident = session.get(OutageIncident, boundary_id)
    if incident is not None:
        if (
            incident.detection_source == OPERATOR_SOURCE
            and getattr(incident, "status", None) == "open"
        ):
            return True
        if (
            incident.detection_source == CLASSIFIER_SOURCE
            and getattr(incident, "status", None)
            in CLASSIFIER_CUSTOMER_VISIBLE_STATUSES
        ):
            return True
        return False
    node = session.get(NetworkDevice, boundary_id)
    if node is None:
        return False
    impact = affected_customers(session, node=node)
    # Same dead-man reading as every other customer-facing consumer: a frozen
    # live_status must not decide whether an outage notification goes out.
    loc = localize_outage(
        session, impact["node_ids"], now=now, warm_stale=warmer_is_stale(now)
    )
    if loc is None or loc["class"] != NODE_OUTAGE:
        return False
    if loc["confidence"] != "high":
        return False
    return loc["affected_online_before"] >= _area_min_affected()


def _notification_incident(
    session: Session, incident_id: uuid.UUID | str | None
) -> OutageIncident | None:
    """Classifier incident allowed to send customer outage notifications."""
    if incident_id is None:
        return None
    try:
        iid = uuid.UUID(str(incident_id))
    except (ValueError, TypeError):
        return None
    incident = session.get(OutageIncident, iid)
    if incident is None:
        return None
    if incident.detection_source != CLASSIFIER_SOURCE:
        return None
    if incident.status not in CLASSIFIER_CUSTOMER_VISIBLE_STATUSES:
        return None
    return incident


def plan_outage_notifications(
    session: Session,
    subscription_ids,
    *,
    incident_id: uuid.UUID | str | None = None,
    now: datetime | None = None,
) -> dict:
    """Read-only preview of what a dispatch WOULD do (no emit, no audit write).

    Reads the persisted debounce + confidence gate so the preview matches a real
    run. ``would_send_total`` is 0 when the feature is disabled and is capped at
    the per-run limit.
    """
    now = now or datetime.now(UTC)
    enabled = _enabled()
    incident = _notification_incident(session, incident_id)
    area_map, per_map = _collect(session, subscription_ids, now)
    if incident_id is not None:
        area_map = {incident.id: area_map.get(incident.id, [])} if incident else {}
        per_map = {}

    would = 0
    area_out = []
    for boundary_id, targets in area_map.items():
        qualifies = _area_boundary_qualifies(session, boundary_id, now)
        debounced = _boundary_debounced(session, boundary_id, now)
        recipients = sum(1 for t in targets if t.opted_in and t.email)
        if enabled and qualifies and not debounced:
            would += recipients
        area_out.append(
            {
                "boundary_id": str(boundary_id),
                "recipients": recipients,
                "suppressed_optout": sum(1 for t in targets if not t.opted_in),
                "qualifies": qualifies,
                "debounced": debounced,
                "sample_body": targets[0].message if targets else "",
            }
        )

    per_out = []
    for verdict, targets in per_map.items():
        recipients = sum(
            1
            for t in targets
            if t.opted_in
            and t.email
            and not _dedup_debounced(session, f"pc:{t.subscriber_id}:{verdict}", now)
        )
        if enabled:
            would += recipients
        per_out.append(
            {
                "verdict": verdict,
                "recipients": recipients,
                "suppressed_optout": sum(1 for t in targets if not t.opted_in),
                "sample_body": targets[0].message if targets else "",
            }
        )

    would = min(would, _max_per_run()) if enabled else 0
    plan = {
        "enabled": enabled,
        "dry_run": True,
        "dispatched": False,
        "generated_at": now.isoformat(),
        "incident_id": str(incident.id) if incident is not None else None,
        "incident_valid": incident is not None if incident_id is not None else None,
        "area_outages": area_out,
        "per_customer": per_out,
        "would_send_total": would,
    }
    logger.info(
        "outage-notify PLAN: enabled=%s areas=%d per_customer=%d would_send=%d "
        "(preview only — dispatched=False)",
        enabled,
        len(area_out),
        len(per_out),
        would,
    )
    return plan


def _emit(
    session: Session, type_value: str, target: _Target, subject: str, actor_id
) -> bool:
    """Hand one notification to the notification system (it picks the channels).
    Returns True on a clean emit, False if the emit raised."""
    from app.services.events import emit_event
    from app.services.events.types import EventType

    try:
        emit_event(
            session,
            getattr(EventType, type_value),
            {
                "email": target.email,
                "subscriber_name": target.subscriber_name,
                "message": target.message,
                "subject": subject,
            },
            actor=str(actor_id),
            subscriber_id=target.subscriber_id,
            subscription_id=target.subscription_id,
        )
        return True
    except Exception:
        logger.warning(
            "outage-notify emit failed for subscription %s",
            target.subscription_id,
            exc_info=True,
        )
        return False


def dispatch_outage_notifications(
    session: Session,
    subscription_ids,
    *,
    actor_id: uuid.UUID | None,
    incident_id: uuid.UUID | str | None = None,
    now: datetime | None = None,
) -> dict:
    """Actually dispatch outage notifications — hard-gated, idempotent, audited.

    Requires ALL of: ``OUTAGE_NOTIFY_ENABLED`` on; an explicit operator
    ``actor_id`` (no auto-trigger exists); per boundary the confidence gate +
    persisted debounce; and the per-run cap. When disabled or without an actor
    it is a pure no-op that returns the preview plan. Every attempt writes an
    audit row (which is also the debounce source). Delegates channel selection
    and final delivery to the notification system via ``emit_event``.
    """
    now = now or datetime.now(UTC)
    if not _enabled() or actor_id is None:
        plan = plan_outage_notifications(
            session, subscription_ids, incident_id=incident_id, now=now
        )
        plan["dispatched"] = False
        plan["reason"] = "disabled" if not _enabled() else "no_actor"
        return plan

    incident = _notification_incident(session, incident_id)
    if incident is None:
        plan = plan_outage_notifications(
            session, subscription_ids, incident_id=incident_id, now=now
        )
        plan["dispatched"] = False
        plan["reason"] = "invalid_incident"
        return plan

    area_map, per_map = _collect(session, subscription_ids, now)
    area_map = {incident.id: area_map.get(incident.id, [])}
    per_map = {}
    cap = _max_per_run()
    batch = _batch_size()
    counts = {
        _SENT: 0,
        _FAILED: 0,
        _SUPPRESSED_OPTOUT: 0,
        _SKIPPED_DEBOUNCE: 0,
        _SKIPPED_LOW_CONFIDENCE: 0,
        _SKIPPED_CAP: 0,
        _SKIPPED_NO_RECIPIENT: 0,
        _SKIPPED_INVALID_INCIDENT: 0,
    }
    sent = 0

    def _audit(**kw) -> None:
        session.add(
            OutageNotificationDispatch(
                category=OUTAGE_NOTIFY_CATEGORY.value,
                actor_id=actor_id,
                **kw,
            )
        )
        counts[kw["status"]] = counts.get(kw["status"], 0) + 1

    # --- area outages --------------------------------------------------------
    for boundary_id, targets in area_map.items():
        if not _area_boundary_qualifies(session, boundary_id, now):
            _audit(
                scope="area",
                boundary_node_id=boundary_id,
                channel=_TYPE_AREA,
                dedup_key=f"area:{boundary_id}",
                status=_SKIPPED_LOW_CONFIDENCE,
            )
            continue
        if _boundary_debounced(session, boundary_id, now):
            _audit(
                scope="area",
                boundary_node_id=boundary_id,
                channel=_TYPE_AREA,
                dedup_key=f"area:{boundary_id}",
                status=_SKIPPED_DEBOUNCE,
            )
            continue
        for t in targets:
            base = dict(
                scope="area",
                boundary_node_id=boundary_id,
                subscriber_id=t.subscriber_id,
                subscription_id=t.subscription_id,
                channel=_TYPE_AREA,
                recipient=t.email,
                subject="Service interruption in your area",
                dedup_key=f"area:{boundary_id}:{t.subscriber_id}",
            )
            if not t.opted_in:
                _audit(status=_SUPPRESSED_OPTOUT, **base)
                continue
            if not t.email:
                _audit(status=_SKIPPED_NO_RECIPIENT, **base)
                continue
            if sent >= cap:
                _audit(status=_SKIPPED_CAP, **base)
                continue
            ok = _emit(
                session, _TYPE_AREA, t, "Service interruption in your area", actor_id
            )
            sent += 1
            _audit(status=_SENT if ok else _FAILED, **base)
            if sent % batch == 0:
                logger.info("outage-notify: %d emitted so far (cap %d)", sent, cap)

    # --- per-customer last-mile (operator-targeted only) ---------------------
    for verdict, targets in per_map.items():
        for t in targets:
            dk = f"pc:{t.subscriber_id}:{verdict}"
            base = dict(
                scope="per_customer",
                subscriber_id=t.subscriber_id,
                subscription_id=t.subscription_id,
                channel=_TYPE_LAST_MILE,
                recipient=t.email,
                subject="About your connection",
                dedup_key=dk,
            )
            if not t.opted_in:
                _audit(status=_SUPPRESSED_OPTOUT, **base)
                continue
            if not t.email:
                _audit(status=_SKIPPED_NO_RECIPIENT, **base)
                continue
            if _dedup_debounced(session, dk, now):
                _audit(status=_SKIPPED_DEBOUNCE, **base)
                continue
            if sent >= cap:
                _audit(status=_SKIPPED_CAP, **base)
                continue
            ok = _emit(session, _TYPE_LAST_MILE, t, "About your connection", actor_id)
            sent += 1
            _audit(status=_SENT if ok else _FAILED, **base)
            if sent % batch == 0:
                logger.info("outage-notify: %d emitted so far (cap %d)", sent, cap)

    session.flush()
    result = {
        "enabled": True,
        "dispatched": True,
        "generated_at": now.isoformat(),
        "incident_id": str(incident.id),
        "sent_total": counts[_SENT],
        "counts": counts,
    }
    logger.info("outage-notify DISPATCH by actor=%s: %s", actor_id, counts)
    return result


def recent_dispatches(
    session: Session, boundary_node_id: uuid.UUID, *, limit: int = 25
) -> dict:
    """Recent dispatch audit rows for one area boundary (operator console view).

    Read-only. Returns the most recent ``OutageNotificationDispatch`` rows for
    this boundary (newest first) plus a status tally, so an operator can see who
    was notified, when, by whom, and why anything was suppressed/debounced.
    Per-customer rows (``boundary_node_id is None``) are not shown here — this is
    the area view keyed on the boundary.
    """
    rows = (
        session.query(OutageNotificationDispatch)
        .filter(OutageNotificationDispatch.boundary_node_id == boundary_node_id)
        .order_by(OutageNotificationDispatch.created_at.desc())
        .limit(limit)
        .all()
    )
    counts: dict[str, int] = {}
    out = []
    for r in rows:
        counts[r.status] = counts.get(r.status, 0) + 1
        out.append(
            {
                "created_at": r.created_at,
                "status": r.status,
                "scope": r.scope,
                "recipient": r.recipient,
                "subject": r.subject,
                "actor_id": str(r.actor_id) if r.actor_id else None,
            }
        )
    return {"rows": out, "counts": counts}
