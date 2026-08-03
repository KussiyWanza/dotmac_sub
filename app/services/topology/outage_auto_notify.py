"""Automated trigger for customer outage notifications (ADR 0004).

``outage_notifications`` deliberately had no auto-trigger: dispatch required a
human clicking ``/admin/network/detected-outages/notify``. In production nobody
ever clicked it — 3,723 incidents were detected in 17 days and
``outage_notification_dispatches`` stayed empty — so customers learned about
outages by contacting support. This module supplies the trigger the design
withheld, under a narrower gate than the manual path.

**This module does not decide who to notify, what to say, or on which channel.**
It selects which incidents are automation-eligible and then calls
``dispatch_outage_notifications``, which keeps every guard it already had
(confidence gate, persisted debounce, per-run cap, opt-out, audit row). Channel
selection remains ``notification_channel_policy``'s.

Automation is narrower than a human dispatch on purpose:

* off by default (``outage_auto_notify_enabled``)
* classifier ``node_outage`` only — ``radio_cluster`` is too noisy to automate
  (2,252 of 2,459 such production incidents ended ``discarded``)
* the incident must have been customer-visible for a settling period, so a
  blip that self-clears never reaches a customer
* a minimum affected-subscriber count
* a per-run incident cap, independent of the per-run recipient cap below it
* dispatches are stamped with :data:`AUTO_ACTOR_ID` so an auditor can always
  separate automated sends from operator sends
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from app.models.domain_settings import SettingDomain
from app.models.network_monitoring import OutageIncident
from app.services import settings_spec
from app.services.topology.outage import (
    CLASSIFIER_CUSTOMER_VISIBLE_STATUSES,
    CLASSIFIER_SOURCE,
)
from app.services.topology.outage_notifications import (
    dispatch_outage_notifications,
    plan_outage_notifications,
)

logger = logging.getLogger(__name__)

#: Sentinel actor recorded on automated dispatches. ``actor_id`` carries no FK
#: (the audit outlives deleted rows), so a fixed non-person UUID is safe and
#: makes "did a human send this?" answerable from the audit table alone.
AUTO_ACTOR_ID = uuid.UUID("00000000-0000-0000-0000-00074a5e0001")

#: Only this classification is automatable. See module docstring.
AUTOMATABLE_CLASSIFICATION = "node_outage"

#: Single-flight lock for the automated pass. Two concurrent runs would each
#: read the debounce table before the other wrote to it and double-notify.
ADVISORY_LOCK_KEY = 0x6F_61_6E


def _gate(session: Session, key: str, fallback: object) -> object:
    """Read one automation gate.

    Database-authoritative via ``settings_spec`` so an operator can arm, disarm
    or re-tighten automation from the admin UI mid-incident. An env-var-only
    flag would need a deploy to flip, which is the wrong control for something
    that contacts customers.
    """
    value = settings_spec.resolve_value(session, SettingDomain.network_monitoring, key)
    return fallback if value is None else value


def _auto_enabled(session: Session) -> bool:
    return bool(_gate(session, "outage_auto_notify_enabled", False))


def _superseded(session: Session) -> bool:
    """True once ``network.outage_communications`` owns customer messaging.

    Read through this module's own gate seam rather than calling into the new
    owner, so there stays exactly one settings-reading surface per module.
    """
    return bool(_gate(session, "outage_customer_comms_enabled", False))


def _dry_run(session: Session) -> bool:
    return bool(_gate(session, "outage_auto_notify_dry_run", True))


def _settle_period(session: Session) -> timedelta:
    return timedelta(
        minutes=int(_gate(session, "outage_auto_notify_settle_minutes", 15))  # type: ignore[call-overload]
    )


def _min_affected(session: Session) -> int:
    return int(_gate(session, "outage_auto_notify_min_affected", 5))  # type: ignore[call-overload]


def _max_incidents_per_run(session: Session) -> int:
    return int(_gate(session, "outage_auto_notify_max_incidents_per_run", 10))  # type: ignore[call-overload]


def eligible_incidents(
    session: Session, *, now: datetime | None = None
) -> list[OutageIncident]:
    """Incidents an automated run may notify about, oldest first.

    Narrower than what an operator may dispatch manually: an operator can look
    at a low-confidence or small incident and judge it worth sending; the
    scheduler cannot.
    """
    now = now or datetime.now(UTC)
    cutoff = now - _settle_period(session)

    candidates = (
        session.query(OutageIncident)
        .filter(OutageIncident.detection_source == CLASSIFIER_SOURCE)
        .filter(OutageIncident.status.in_(tuple(CLASSIFIER_CUSTOMER_VISIBLE_STATUSES)))
        .filter(OutageIncident.classification == AUTOMATABLE_CLASSIFICATION)
        .order_by(OutageIncident.created_at.asc())
        .all()
    )

    eligible: list[OutageIncident] = []
    for incident in candidates:
        # Re-assert the SQL predicates in Python. Belt and braces: these are the
        # gates that decide whether a real customer is contacted, and they
        # should not be reachable only through a WHERE clause.
        if incident.detection_source != CLASSIFIER_SOURCE:
            continue
        if incident.status not in CLASSIFIER_CUSTOMER_VISIBLE_STATUSES:
            continue
        if incident.classification != AUTOMATABLE_CLASSIFICATION:
            continue
        if (incident.affected_count or 0) < _min_affected(session):
            continue
        # Settling: use the moment the incident became customer-visible when we
        # have it, else when it was first seen. A fault that clears inside the
        # window never reaches a customer.
        visible_at = incident.confirmed_at or incident.created_at
        if visible_at is None:
            continue
        if visible_at.tzinfo is None:
            visible_at = visible_at.replace(tzinfo=UTC)
        if visible_at > cutoff:
            continue
        eligible.append(incident)

    return eligible


def auto_dispatch_due_outage_notifications(
    session: Session,
    *,
    now: datetime | None = None,
    subscription_ids_for: Callable[[Session, OutageIncident], list[uuid.UUID]]
    | None = None,
) -> dict:
    """One automated pass. Returns a per-incident summary.

    Owns its transaction: the calling task is an adapter and must not commit
    (tests/architecture/test_adapter_transaction_ownership.py). A failure rolls
    back rather than leaving audit rows half-written — those rows are also the
    debounce source, so a partial write would mute a boundary that was never
    actually notified.

    A no-op returning ``dispatched: False`` when the automation flag is off, so
    the task can be scheduled before the decision to enable it is made.
    ``subscription_ids_for`` is injectable so tests need not build topology.
    """
    try:
        result = _run_auto_dispatch(
            session, now=now, subscription_ids_for=subscription_ids_for
        )
    except Exception as exc:  # noqa: BLE001 - report and roll back
        session.rollback()
        logger.exception("outage_auto_notify_failed")
        return {"dispatched": False, "reason": "error", "error": str(exc)}
    session.commit()
    return result


def _run_auto_dispatch(
    session: Session,
    *,
    now: datetime | None = None,
    subscription_ids_for: Callable[[Session, OutageIncident], list[uuid.UUID]]
    | None = None,
) -> dict:
    now = now or datetime.now(UTC)

    if not _auto_enabled(session):
        return {"dispatched": False, "reason": "auto_disabled", "incidents": []}

    # One canonical customer outage sender. Once the spine-driven owner is
    # armed it decides every customer message (including the restoration this
    # path never had), so this automation stands down rather than sending a
    # second opening message from a different audience calculation.
    if _superseded(session):
        return {
            "dispatched": False,
            "reason": "superseded_by_outage_communications",
            "incidents": [],
        }

    if subscription_ids_for is None:
        from app.services.topology.outage_targets import incident_subscription_ids

        subscription_ids_for = incident_subscription_ids

    results: list[dict] = []
    cap = _max_incidents_per_run(session)
    dry_run = _dry_run(session)

    for incident in eligible_incidents(session, now=now)[:cap]:
        sub_ids = subscription_ids_for(session, incident)
        if not sub_ids:
            results.append(
                {
                    "incident_id": str(incident.id),
                    "dispatched": False,
                    "reason": "no_affected_subscriptions",
                }
            )
            continue

        if dry_run:
            plan = plan_outage_notifications(
                session, sub_ids, incident_id=incident.id, now=now
            )
            plan.update(
                {
                    "incident_id": str(incident.id),
                    "dispatched": False,
                    "reason": "dry_run",
                }
            )
            logger.info(
                "outage_auto_notify_dry_run",
                extra={
                    "event": "outage_auto_notify_dry_run",
                    "incident_id": str(incident.id),
                    "would_notify": len(sub_ids),
                },
            )
            results.append(plan)
            continue

        # Every guard inside dispatch still applies; automation only supplies
        # the trigger and an auditable non-human actor.
        result = dispatch_outage_notifications(
            session,
            sub_ids,
            actor_id=AUTO_ACTOR_ID,
            incident_id=incident.id,
            now=now,
        )
        result["incident_id"] = str(incident.id)
        results.append(result)
        logger.info(
            "outage_auto_notify_dispatched",
            extra={
                "event": "outage_auto_notify_dispatched",
                "incident_id": str(incident.id),
                "counts": result.get("counts"),
            },
        )

    return {
        "dispatched": not dry_run,
        "reason": "dry_run" if dry_run else "ok",
        "incidents": results,
    }
