"""Per-subscription service-impact resolver.

Owner: ``network.service_impact`` (docs/designs/OUTAGE_SLA_SPINE.md §1).
Topology establishes exposure, never downtime: audience membership proves a
subscription's path traverses the failed boundary; the incident lifecycle
supplies the provider-fault evidence word; live RADIUS sessions supply
continued-service proof that prevents accrual. This resolver is read-only —
it decides the six-state impact word per subscription and returns typed
evidence, but persists nothing, sends nothing, and never turns a lone dark
endpoint or stale telemetry into confirmed downtime:

- a shared-boundary incident that is confirmed covers its exact current
  audience as ``confirmed_unavailable`` unless a live session proves
  continued service (then ``potentially_affected`` — exposed, not accruing);
- a ``suspected`` incident is exposure only (``potentially_affected``);
- ``clearing`` renders ``restored`` for members observed back online and
  ``unknown`` for members still dark (the boundary recovered; what remains
  is individual and unproven);
- without a covering incident, a single failure observation is insufficient
  by policy — the resolver answers ``unknown`` with the observation attached,
  never ``confirmed_unavailable`` (two independent observations or a
  reviewed provider-fault ticket arrive with later slices);
- ``excluded`` is reserved for the maintenance owner (later slice) and is
  never produced here.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.models.network_monitoring import OutageIncident
from app.models.radius_active_session import RadiusActiveSession
from app.services.service_impact_contracts import (
    ImpactEvidence,
    ImpactEvidenceKind,
    ImpactState,
)
from app.services.topology.outage import (
    OutageStatus,
    latest_scope_revision,
    list_open_incidents,
    record_scope_revision,
    revision_audience_subscription_ids,
)

logger = logging.getLogger(__name__)

_CONFIRMED_STATUSES = frozenset({OutageStatus.open.value, OutageStatus.confirmed.value})
_CLEARING_STATUS = OutageStatus.clearing.value
_SUSPECTED_STATUS = OutageStatus.suspected.value


@dataclass(frozen=True, slots=True)
class SubscriptionImpact:
    """One subscription's resolved impact word with its typed evidence."""

    subscription_id: str
    state: ImpactState
    reason: str
    evaluated_at: datetime
    incident_id: str | None = None
    scope_revision_sequence: int | None = None
    membership_token: str | None = None
    evidence: tuple[ImpactEvidence, ...] = ()


@dataclass(frozen=True, slots=True)
class IncidentImpactSummary:
    """Bounded counts per state for one incident's current audience."""

    incident_id: str
    audience_count: int
    confirmed_unavailable: int
    potentially_affected: int
    restored: int
    unknown: int


def _incident_evidence(
    incident: OutageIncident, revision_sequence: int | None
) -> ImpactEvidence:
    observed_at = incident.confirmed_at or incident.started_at
    if observed_at is not None and observed_at.tzinfo is None:
        observed_at = observed_at.replace(tzinfo=UTC)
    return ImpactEvidence(
        kind=ImpactEvidenceKind.shared_boundary_failure,
        owner="network.outage_lifecycle",
        observed_at=observed_at or datetime.now(UTC),
        reference=(
            f"incident:{incident.id}"
            + (f"#rev{revision_sequence}" if revision_sequence else "")
        ),
        detail=f"status={incident.status}",
    )


def _live_session_evidence(observed_at: datetime) -> ImpactEvidence:
    return ImpactEvidence(
        kind=ImpactEvidenceKind.independent_observation,
        owner="network.radius_sessions",
        observed_at=observed_at,
        reference="radius:active-session",
        detail="live session proves continued service",
    )


def _online_subscription_ids(db: Session, subscription_ids: set[str]) -> set[str]:
    """Members with a live RADIUS session — the continued-service proof."""

    if not subscription_ids:
        return set()
    rows = (
        db.query(RadiusActiveSession.subscription_id)
        .filter(RadiusActiveSession.subscription_id.in_(list(subscription_ids)))
        .all()
    )
    return {str(row[0]) for row in rows if row[0] is not None}


def resolve_incident_impacts(
    db: Session,
    incident: OutageIncident,
    *,
    now: datetime | None = None,
) -> tuple[SubscriptionImpact, ...]:
    """Classify the incident's exact current audience, one word per member.

    The audience comes from the latest immutable scope revision (recorded on
    the spot for pre-revision incidents), never re-guessed from names.
    """

    evaluated_at = now or datetime.now(UTC)
    revision = latest_scope_revision(db, incident.id)
    if revision is None:
        revision = record_scope_revision(
            db, incident, reason="audience_drift", effective_at=evaluated_at
        )
    if revision is None:
        return ()
    audience = revision_audience_subscription_ids(revision)
    online = _online_subscription_ids(db, audience)
    boundary = _incident_evidence(incident, revision.sequence)

    impacts: list[SubscriptionImpact] = []
    for subscription_id in sorted(audience):
        is_online = subscription_id in online
        if incident.status == _SUSPECTED_STATUS:
            state, reason = ImpactState.potentially_affected, "incident_suspected"
        elif incident.status in _CONFIRMED_STATUSES:
            if is_online:
                state, reason = (
                    ImpactState.potentially_affected,
                    "continued_service_observed",
                )
            else:
                state, reason = (
                    ImpactState.confirmed_unavailable,
                    "shared_boundary_confirmed",
                )
        elif incident.status == _CLEARING_STATUS:
            if is_online:
                state, reason = ImpactState.restored, "recovery_observed"
            else:
                state, reason = (
                    ImpactState.unknown,
                    "boundary_recovered_endpoint_dark",
                )
        else:
            # Terminal incidents carry no live impact; callers filter first.
            continue
        evidence: tuple[ImpactEvidence, ...] = (boundary,)
        if is_online:
            evidence = (boundary, _live_session_evidence(evaluated_at))
        impacts.append(
            SubscriptionImpact(
                subscription_id=subscription_id,
                state=state,
                reason=reason,
                evaluated_at=evaluated_at,
                incident_id=str(incident.id),
                scope_revision_sequence=revision.sequence,
                membership_token=revision.membership_token,
                evidence=evidence,
            )
        )
    return tuple(impacts)


def summarize_incident_impact(
    db: Session,
    incident: OutageIncident,
    *,
    now: datetime | None = None,
) -> IncidentImpactSummary:
    """State counts for one incident — the exposure-vs-confirmed split."""

    impacts = resolve_incident_impacts(db, incident, now=now)
    counts = dict.fromkeys(ImpactState, 0)
    for impact in impacts:
        counts[impact.state] += 1
    return IncidentImpactSummary(
        incident_id=str(incident.id),
        audience_count=len(impacts),
        confirmed_unavailable=counts[ImpactState.confirmed_unavailable],
        potentially_affected=counts[ImpactState.potentially_affected],
        restored=counts[ImpactState.restored],
        unknown=counts[ImpactState.unknown],
    )


def resolve_subscription_impact(
    db: Session,
    subscription_id,
    *,
    now: datetime | None = None,
) -> SubscriptionImpact | None:
    """One subscription's live impact, or None when no live incident covers it.

    None is the honest steady-state answer: without a covering incident this
    resolver never manufactures a word from a lone observation — individual
    last-mile confirmation needs two independent observations or a reviewed
    provider-fault ticket (later slices).
    """

    wanted = str(subscription_id)
    for incident in list_open_incidents(db):
        revision = latest_scope_revision(db, incident.id)
        if revision is None:
            continue
        if wanted not in revision_audience_subscription_ids(revision):
            continue
        for impact in resolve_incident_impacts(db, incident, now=now):
            if impact.subscription_id == wanted:
                return impact
    return None
