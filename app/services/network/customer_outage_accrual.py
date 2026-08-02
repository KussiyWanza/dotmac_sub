"""Immutable per-subscription downtime ledger.

Owner: ``network.customer_outage_accrual`` (OUTAGE_SLA_SPINE §2, §7). The
sole writer of ``customer_outage_intervals``. It reconciles the impact
resolver's words into intervals under the approved clock rules:

- an interval opens when a member is ``confirmed_unavailable``, starting at
  the incident's earliest qualifying observation (``started_at``) for the
  original audience, or at the scope-revision ``effective_at`` where a
  mid-incident member entered — audience entry never rewrites history;
- the interval ends provisionally at the first healthy observation
  (continued-service or recovery evidence); re-darkening before finalization
  clears the provisional end, so clearing→reopened stays one continuous
  interval;
- termination finalizes: resolved incidents close at the proven recovery
  timestamp (``cleared_at``, falling back to ``resolved_at``) and stamp
  ``finalized_at``; discarded incidents finalize with
  ``exclusion_candidate='incident_discarded'`` — recorded, reviewed, never
  silently dropped;
- ``resolved_at`` alone never determines downtime, unknown never accrues,
  and reconciliation is idempotent — reruns converge with no duplicate or
  overlapping rows (the partial unique open-interval index enforces it at
  the database too).

Delivery: the outage lifecycle projection handler invokes the receipted
``consume_accrual_event`` per committed lifecycle output; a redelivery is an
exact no-op via the (consumer, event_id) receipt.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.models.network_monitoring import (
    CustomerOutageInterval,
    OutageIncident,
    OutageScopeRevision,
    OutageScopeRevisionMember,
)
from app.services.network.service_impact import resolve_incident_impacts
from app.services.service_impact_contracts import ImpactState
from app.services.topology.outage import OutageStatus

logger = logging.getLogger(__name__)

_DISCARD_EXCLUSION = "incident_discarded"


@dataclass(frozen=True, slots=True)
class AccrualReconcileResult:
    """What one idempotent reconcile pass changed."""

    incident_id: str
    opened: int = 0
    provisionally_ended: int = 0
    reopened: int = 0
    finalized: int = 0

    @property
    def changed(self) -> bool:
        return bool(
            self.opened + self.provisionally_ended + self.reopened + self.finalized
        )


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def _open_intervals(db: Session, incident_id) -> dict[str, CustomerOutageInterval]:
    rows = (
        db.query(CustomerOutageInterval)
        .filter(
            CustomerOutageInterval.incident_id == incident_id,
            CustomerOutageInterval.finalized_at.is_(None),
        )
        .all()
    )
    return {str(row.subscription_id): row for row in rows}


def _member_entry_time(
    db: Session, incident: OutageIncident, subscription_id: str
) -> datetime:
    """When this member's exposure began: incident start for the original
    audience, the entering revision's effective_at for later joiners."""

    entered = (
        db.query(OutageScopeRevision)
        .join(
            OutageScopeRevisionMember,
            OutageScopeRevisionMember.revision_id == OutageScopeRevision.id,
        )
        .filter(
            OutageScopeRevision.incident_id == incident.id,
            OutageScopeRevisionMember.subscription_id == subscription_id,
            OutageScopeRevisionMember.membership == "entered",
        )
        .order_by(OutageScopeRevision.sequence.desc())
        .first()
    )
    started = _utc(incident.started_at) or datetime.now(UTC)
    if entered is None or entered.sequence == 1:
        return started
    effective = _utc(entered.effective_at) or started
    return max(started, effective)


def reconcile_incident_accrual(
    db: Session,
    incident: OutageIncident,
    *,
    now: datetime | None = None,
) -> AccrualReconcileResult:
    """Idempotently converge the ledger with the incident's current impacts."""

    evaluated_at = now or datetime.now(UTC)
    open_intervals = _open_intervals(db, incident.id)
    opened = provisionally_ended = reopened = finalized = 0

    if incident.status in (
        OutageStatus.resolved.value,
        OutageStatus.discarded.value,
    ):
        discarded = incident.status == OutageStatus.discarded.value
        recovery_at = (
            _utc(incident.cleared_at) or _utc(incident.resolved_at) or evaluated_at
        )
        for open_interval in open_intervals.values():
            if open_interval.ended_at is None:
                open_interval.ended_at = recovery_at
                open_interval.recovery_evidence_ref = (
                    f"incident:{incident.id}:{incident.status}"
                )
                provisionally_ended += 1
            if discarded and open_interval.exclusion_candidate is None:
                open_interval.exclusion_candidate = _DISCARD_EXCLUSION
            open_interval.finalized_at = evaluated_at
            finalized += 1
        db.flush()
        return AccrualReconcileResult(
            incident_id=str(incident.id),
            provisionally_ended=provisionally_ended,
            finalized=finalized,
        )

    impacts = resolve_incident_impacts(db, incident, now=evaluated_at)
    impacted_ids: set[str] = set()
    for impact in impacts:
        impacted_ids.add(impact.subscription_id)
        interval: CustomerOutageInterval | None = open_intervals.get(
            impact.subscription_id
        )
        if impact.state is ImpactState.confirmed_unavailable:
            if interval is None:
                from app.services.network.maintenance_lifecycle import (
                    exclusion_candidate_for_incident,
                )

                start = _member_entry_time(db, incident, impact.subscription_id)
                first_ref = impact.evidence[0].reference if impact.evidence else None
                db.add(
                    CustomerOutageInterval(
                        incident_id=incident.id,
                        subscription_id=impact.subscription_id,
                        state=ImpactState.confirmed_unavailable.value,
                        quality="exact",
                        started_at=start,
                        scope_revision_sequence=(impact.scope_revision_sequence or 1),
                        first_evidence_ref=first_ref,
                        # A properly announced maintenance window covering the
                        # start makes this a reviewed exclusion candidate —
                        # recorded, never silently dropped.
                        exclusion_candidate=exclusion_candidate_for_incident(
                            db, incident, at=start
                        ),
                        idempotency_key=(
                            f"{incident.id}:{impact.subscription_id}:"
                            f"{int(start.timestamp())}"
                        ),
                    )
                )
                opened += 1
            elif interval.ended_at is not None:
                # Re-darkened before finalization: one continuous interval.
                interval.ended_at = None
                interval.recovery_evidence_ref = None
                reopened += 1
        else:
            # potentially_affected / restored / unknown: the member is not
            # accruing — a first healthy or evidence-loss observation ends
            # any open interval provisionally.
            if interval is not None and interval.ended_at is None:
                interval.ended_at = evaluated_at
                interval.recovery_evidence_ref = (
                    impact.evidence[-1].reference
                    if impact.evidence
                    else f"impact:{impact.state.value}"
                )
                provisionally_ended += 1

    # Members that left the audience entirely stop accruing at evaluation
    # time — audience exit clamps the interval without rewriting history.
    for subscription_id, interval in open_intervals.items():
        if subscription_id not in impacted_ids and interval.ended_at is None:
            interval.ended_at = evaluated_at
            interval.recovery_evidence_ref = "audience:left"
            provisionally_ended += 1

    db.flush()
    return AccrualReconcileResult(
        incident_id=str(incident.id),
        opened=opened,
        provisionally_ended=provisionally_ended,
        reopened=reopened,
        finalized=finalized,
    )


def _consume_definition(name: str):
    from app.services.owner_commands import OwnerCommandDefinition

    return OwnerCommandDefinition(
        owner="network.customer_outage_accrual",
        concern="committed outage output accrual consumption",
        name=name,
    )


def consume_accrual_event(
    session: Session,
    *,
    incident_id,
    event_id,
    event_type: str,
    context,
) -> str | None:
    """Receipt one committed lifecycle output into ledger reconciliation."""

    from app.services.events.owner_outputs import consume_owner_output
    from app.services.owner_commands import execute_owner_command

    def _effect() -> str:
        incident = session.get(OutageIncident, incident_id)
        if incident is None:
            logger.warning(
                "outage accrual consequence: incident %s missing", incident_id
            )
            return "skipped_missing"
        result = reconcile_incident_accrual(session, incident)
        return "applied" if result.changed else "noop"

    return execute_owner_command(
        session,
        definition=_consume_definition("consume_accrual_event"),
        context=context,
        operation=lambda: consume_owner_output(
            session,
            consumer="network.customer_outage_accrual",
            event_id=event_id,
            event_type=event_type,
            producer_owner="network.outage_lifecycle",
            context=context,
            operation=_effect,
        )[0],
    )


def intervals_for_incident(db: Session, incident_id) -> list[CustomerOutageInterval]:
    return (
        db.query(CustomerOutageInterval)
        .filter(CustomerOutageInterval.incident_id == incident_id)
        .order_by(
            CustomerOutageInterval.subscription_id,
            CustomerOutageInterval.started_at,
        )
        .all()
    )


def intervals_for_subscription(
    db: Session,
    subscription_id,
    *,
    since: datetime | None = None,
) -> list[CustomerOutageInterval]:
    query = db.query(CustomerOutageInterval).filter(
        CustomerOutageInterval.subscription_id == subscription_id
    )
    if since is not None:
        query = query.filter(
            (CustomerOutageInterval.ended_at.is_(None))
            | (CustomerOutageInterval.ended_at >= since)
        )
    return query.order_by(CustomerOutageInterval.started_at).all()
