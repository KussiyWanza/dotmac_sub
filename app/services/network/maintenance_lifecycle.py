"""Planned maintenance lifecycle owner.

Owner: ``network.maintenance_lifecycle`` (OUTAGE_SLA_SPINE §5). Sole writer
of ``network_maintenance_windows``: draft → approved → announced →
in_progress → completed, plus canceled and overrun. Every transition stages
its typed ``maintenance.*`` output atomically with the status write, exactly
like the outage lifecycle.

Approved policy encoded here:

- at least ``MAINTENANCE_NOTICE_DAYS`` calendar days between announcement and
  the planned start for the window to be SLA-excludable — announcing late
  still records the notice honestly, it just never excludes;
- the audience is resolved at announce and re-resolved at begin; a changed
  membership token is material scope drift and requires explicit renewed
  approval, never a silent override;
- monitoring continues during maintenance; only the approved, properly
  notified planned window is excludable — unannounced work, newly affected
  customers, and overrun time count as unplanned downtime;
- an unresolved interruption at the scheduled end becomes an outage:
  ``escalate_overrun_to_outage`` declares one through the outage lifecycle
  owner and links it, so accrual and communications flow through the normal
  incident chain;
- emergency maintenance is unplanned by default (no announcement, no
  exclusion).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from app.models.network import FdhCabinet
from app.models.network_monitoring import (
    MaintenanceWindow,
    NetworkDevice,
    OutageIncident,
    PopSite,
)
from app.services.service_impact_contracts import (
    MAINTENANCE_NOTICE_DAYS,
    MaintenanceState,
)

logger = logging.getLogger(__name__)

PLANNED_MAINTENANCE_EXCLUSION = "planned_maintenance"

_SCOPE_KWARGS = {
    "node": "node",
    "basestation": "basestation",
    "fdh-cabinet": "fdh",
}


def _emit_maintenance_event(
    session: Session, window: MaintenanceWindow, kind: str
) -> None:
    from app.services.events import emit_event
    from app.services.events.types import EventType

    payload = {
        "alert_type": kind,
        "maintenance_window_id": str(window.id),
        "status": window.status,
        "scope": {"type": window.scope_type, "id": str(window.scope_id)},
        "planned_start": window.planned_start.isoformat()
        if window.planned_start
        else None,
        "planned_end": window.planned_end.isoformat() if window.planned_end else None,
        "announced_at": window.announced_at.isoformat()
        if window.announced_at
        else None,
        "audience_count": window.audience_count,
        "linked_outage_incident_id": (
            str(window.linked_outage_incident_id)
            if window.linked_outage_incident_id
            else None
        ),
    }
    emit_event(session, EventType(kind), payload, actor=window.owner or "system")


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def _scope_target(session: Session, window: MaintenanceWindow):
    if window.scope_type == "node":
        return {"node": session.get(NetworkDevice, window.scope_id)}
    if window.scope_type == "basestation":
        return {"basestation": session.get(PopSite, window.scope_id)}
    if window.scope_type == "fdh-cabinet":
        return {"fdh": session.get(FdhCabinet, window.scope_id)}
    return {}


def _resolve_audience(session: Session, window: MaintenanceWindow) -> tuple[str, int]:
    from app.services.bulk_actions import membership_scope_token
    from app.services.topology.affected import affected_customers

    target = _scope_target(session, window)
    subscription_ids: list[str] = []
    resolved = next(iter(target.values()), None)
    if resolved is not None:
        impact = affected_customers(session, **target)
        subscription_ids = sorted(
            {str(subscription.id) for subscription in impact["subscriptions"]}
        )
    token = membership_scope_token(
        f"{window.scope_type}:{window.scope_id}", subscription_ids
    )
    return token, len(subscription_ids)


def _require_status(window: MaintenanceWindow, *allowed: MaintenanceState) -> None:
    if window.status not in {state.value for state in allowed}:
        raise ValueError(
            f"maintenance window {window.id} is {window.status}; expected one of "
            f"{sorted(state.value for state in allowed)}"
        )


def create_maintenance_window(
    session: Session,
    *,
    node: NetworkDevice | None = None,
    basestation: PopSite | None = None,
    fdh: FdhCabinet | None = None,
    planned_start: datetime,
    planned_end: datetime,
    reason: str,
    owner: str,
    expected_impact: str | None = None,
    customer_message: str | None = None,
    backout_plan: str | None = None,
) -> MaintenanceWindow:
    """Open a draft window against exactly one infrastructure scope."""

    targets = [
        ("node", node),
        ("basestation", basestation),
        ("fdh-cabinet", fdh),
    ]
    chosen = [(kind, target) for kind, target in targets if target is not None]
    if len(chosen) != 1:
        raise ValueError(
            "a maintenance window requires exactly one scope: node, "
            "basestation, or FDH cabinet"
        )
    if planned_end <= planned_start:
        raise ValueError("a maintenance window cannot end before it starts")
    scope_type, target = chosen[0]
    window = MaintenanceWindow(
        scope_type=scope_type,
        scope_id=target.id,
        status=MaintenanceState.draft.value,
        planned_start=planned_start,
        planned_end=planned_end,
        reason=reason,
        owner=owner,
        expected_impact=expected_impact,
        customer_message=customer_message,
        backout_plan=backout_plan,
    )
    session.add(window)
    session.flush()
    return window


def approve_window(
    session: Session, window: MaintenanceWindow, *, approved_by: str
) -> None:
    _require_status(window, MaintenanceState.draft)
    window.status = MaintenanceState.approved.value
    window.approved_by = approved_by
    session.flush()


def announce_window(
    session: Session, window: MaintenanceWindow, *, now: datetime | None = None
) -> None:
    """Record the customer announcement and snapshot the exact audience."""

    _require_status(window, MaintenanceState.approved)
    announced_at = now or datetime.now(UTC)
    token, count = _resolve_audience(session, window)
    window.status = MaintenanceState.announced.value
    window.announced_at = announced_at
    window.audience_token = token
    window.audience_count = count
    session.flush()
    _emit_maintenance_event(session, window, "maintenance.announced")


def begin_window(
    session: Session,
    window: MaintenanceWindow,
    *,
    now: datetime | None = None,
    drift_approved: bool = False,
) -> None:
    """Start work; re-resolve the audience and refuse silent material drift."""

    _require_status(window, MaintenanceState.announced, MaintenanceState.approved)
    token, count = _resolve_audience(session, window)
    if (
        window.audience_token is not None
        and token != window.audience_token
        and not drift_approved
    ):
        raise ValueError(
            "material scope drift since announcement: renewed approval and "
            "notice are required before starting"
        )
    window.audience_token = token
    window.audience_count = count
    window.status = MaintenanceState.in_progress.value
    window.actual_start = now or datetime.now(UTC)
    session.flush()
    _emit_maintenance_event(session, window, "maintenance.started")


def complete_window(
    session: Session, window: MaintenanceWindow, *, now: datetime | None = None
) -> None:
    """Finish work: past the planned end this is an overrun, not a completion."""

    _require_status(window, MaintenanceState.in_progress)
    finished_at = now or datetime.now(UTC)
    window.actual_end = finished_at
    planned_end = _utc(window.planned_end)
    if planned_end is not None and finished_at > planned_end:
        window.status = MaintenanceState.overrun.value
        session.flush()
        _emit_maintenance_event(session, window, "maintenance.overrun")
        return
    window.status = MaintenanceState.completed.value
    session.flush()
    _emit_maintenance_event(session, window, "maintenance.completed")


def cancel_window(
    session: Session,
    window: MaintenanceWindow,
    *,
    now: datetime | None = None,
    reason: str,
) -> None:
    _require_status(
        window,
        MaintenanceState.draft,
        MaintenanceState.approved,
        MaintenanceState.announced,
        MaintenanceState.in_progress,
    )
    window.status = MaintenanceState.canceled.value
    window.actual_end = now or datetime.now(UTC)
    window.reason = f"{window.reason} | canceled: {reason}"[:200]
    session.flush()
    _emit_maintenance_event(session, window, "maintenance.canceled")


def escalate_overrun_to_outage(
    session: Session, window: MaintenanceWindow, *, now: datetime | None = None
) -> OutageIncident:
    """An unresolved interruption past the window becomes an outage.

    Declared through the outage lifecycle owner so accrual, escalations, and
    communications flow through the normal incident chain; the overrun time
    is unplanned downtime by policy.
    """

    from app.services.topology.outage import declare_outage

    _require_status(window, MaintenanceState.overrun)
    if window.linked_outage_incident_id is not None:
        existing = session.get(OutageIncident, window.linked_outage_incident_id)
        if existing is not None:
            return existing
    target = _scope_target(session, window)
    incident = declare_outage(
        session,
        declared_by="system:maintenance-overrun",
        note=f"maintenance window {window.id} overran its approved end",
        **target,
    )
    window.linked_outage_incident_id = incident.id
    session.flush()
    return incident


def notice_satisfied(window: MaintenanceWindow) -> bool:
    """True when customers got the full approved notice before the start."""

    announced_at = _utc(window.announced_at)
    planned_start = _utc(window.planned_start)
    if announced_at is None or planned_start is None:
        return False
    return announced_at <= planned_start - timedelta(days=MAINTENANCE_NOTICE_DAYS)


def exclusion_candidate_for_incident(
    session: Session, incident: OutageIncident, *, at: datetime
) -> str | None:
    """The reviewed exclusion word when a properly announced window covers
    this incident's scope at ``at`` — inside the planned bounds only.

    Overrun time, unannounced work, and short-notice windows return None:
    that downtime is unplanned by policy.
    """

    if incident.fdh_cabinet_id is not None:
        scope_type, scope_id = "fdh-cabinet", incident.fdh_cabinet_id
    elif incident.root_node_id is not None:
        scope_type, scope_id = "node", incident.root_node_id
    elif incident.basestation_id is not None:
        scope_type, scope_id = "basestation", incident.basestation_id
    else:
        return None
    at = _utc(at) or datetime.now(UTC)
    windows = (
        session.query(MaintenanceWindow)
        .filter(
            MaintenanceWindow.scope_type == scope_type,
            MaintenanceWindow.scope_id == scope_id,
            MaintenanceWindow.status.in_(
                (
                    MaintenanceState.announced.value,
                    MaintenanceState.in_progress.value,
                    MaintenanceState.completed.value,
                    MaintenanceState.overrun.value,
                )
            ),
        )
        .all()
    )
    for window in windows:
        planned_start = _utc(window.planned_start)
        planned_end = _utc(window.planned_end)
        if planned_start is None or planned_end is None:
            continue
        if not notice_satisfied(window):
            continue
        if planned_start <= at < planned_end:
            return PLANNED_MAINTENANCE_EXCLUSION
    return None
